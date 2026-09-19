# -*- coding: utf-8 -*-
"""lecture_live 直播回放下载。

教室录播不在 uploads 体系里，走 rms-v5 独立域名，需要单独一套下载逻辑。
实测契约（2026-09）：

    URL: https://rms-v5.xjtu.edu.cn/api/base/orgs/xjtu/captures/<capture>/videos/<cam>/preview?previewToken=<hex>
    响应头: Content-Length / Accept-Ranges: bytes / Etag
    Range: 返回 206 + Content-Range

三条必须遵守的服务端行为：

  ① **别并发。** 同一 previewToken 同时发多个请求会稳定 403，看起来像过期。
     串行发完全正常（实测单连接连读 438MB / 4.6min 无中断）。所以本模块
     全程串行，且失败后不做并发重试。

  ② **服务端会把 Range 对齐到 2MB 边界。** 请求 `bytes=100000000-100199999`
     实际返回 `bytes 100000000-102097151`。必须按响应里的 Content-Range
     取实际长度，否则续传会错位。

  ③ **速度会衰减。** 前 30s 能吃服务端缓存到 8MB/s，之后稳定 ~1.6MB/s
     （实时转码速度）。一节课约 5 分钟。

一个未解之谜：实测 Content-Length 438175558，读到自然 EOF 共 413.7MB（差 5.6%）。
原因不明（容器尾部 / 时间戳对齐都有可能），在拿到官方哈希前不把它当失败。

处理方式：不再静默吞掉，而是显式比对「服务端声明的总长」与「实得字节数」。
差异在 SHORT_TOLERANCE 以内视作正常（实测 5.6% 落在这个区间），只记一条 note；
超出阈值就告警并把 shortfall 写进结果，让调用方能看出这一节可能真的没下完。
校验只能用「比上次多读到多少」这种相对判据 —— 没有官方哈希可用。
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

# 一节课的录像能到 500MB 上下，留点余量
MAX_BYTES = 4 * 1024 * 1024 * 1024
CHUNK = 256 * 1024

# 实得字节数相对 Content-Length 允许少多少而不告警。
# 实测的自然短读约 5.6%，取 8% 留出余量；超出就说明这次真的异常。
SHORT_TOLERANCE = 0.08


def parse_replay(activity_data):
    """从活动详情里抽出可下载的回放。

    返回 [{camera_id, camera_type, url, status}]，按「屏幕录制优先」排序——
    encoder 是正课内容（课件 + 讲解），instructor 是教师机位（人像），
    多数人只要前者。
    """
    det = (activity_data or {}).get("data", {}).get("external_live_detail") or {}
    vids = det.get("replay_videos") or []
    out = []
    for v in vids:
        u = (v.get("url") or "").strip()
        if not u:
            continue
        out.append({
            "camera_id": v.get("camera_id"),
            "camera_type": v.get("camera_type") or "unknown",
            "url": u,
        })
    # encoder 优先，其余保持原序
    out.sort(key=lambda x: 0 if x["camera_type"] == "encoder" else 1)
    return out


def start_stamp(activity):
    """把活动的开始时间转成可读的本地时间戳，形如 20260919-1430。

    ★ 为什么必须带上它：同一天连上几节课时，活动的 title 完全相同
    （实测 4 个 lecture_live 活动都叫「2026-09-19-计算机视觉与模式识别」），
    只用 title 命名会让 4 节课互相覆盖、只剩最后一节。start_time 是唯一
    能区分它们的字段。
    """
    for key in ("start_time", "created_at", "updated_at"):
        raw = (activity or {}).get(key)
        if not raw:
            continue
        try:
            s = str(raw).replace("Z", "+00:00")
            import datetime
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                dt = dt.astimezone()          # 转本地时区
            return dt.strftime("%Y%m%d-%H%M")
        except Exception:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", str(raw))
            if m:
                return "%s%s%s-%s%s" % m.groups()
    return None


def probe(op, url, timeout=45):
    """轻量探测：拿 Content-Length 判断体积，用于干跑。

    只发一个请求，取完头部就断开——不要读 body，否则触发限流。
    """
    req = urllib.request.Request(url)
    for k, v in getattr(op, "addheaders", []) or []:
        req.add_header(k, v)
    try:
        r = op.open(req, timeout=timeout)
        size = r.headers.get("Content-Length")
        try:
            r.close()
        except Exception:
            pass
        return {"ok": True, "size": int(size) if size else None,
                "ranges": bool(r.headers.get("Accept-Ranges"))}
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": "HTTP %d" % e.code, "code": e.code}
    except Exception as e:
        return {"ok": False, "err": str(e)[:80]}


def _open_range(op, url, offset, timeout=120):
    """带 Range 发一次请求。返回 (response, start) 或 (None, err)。

    注意服务端会扩大 Range 到 2MB 边界 —— 返回的 start 可能与请求的不同，
    以 Content-Range 为准。
    """
    req = urllib.request.Request(url)
    for k, v in getattr(op, "addheaders", []) or []:
        req.add_header(k, v)
    if offset:
        req.add_header("Range", "bytes=%d-" % offset)
    r = op.open(req, timeout=timeout)
    status = getattr(r, "status", 200)
    start = offset

    cr = r.headers.get("Content-Range")
    if cr:
        m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", cr.strip())
        if m:
            start = int(m.group(1))
    elif offset and status == 200:
        # 服务端忽略了 Range，返回全量。已下的部分作废，从头来。
        start = 0
    return r, start, status


def download(op, url, path, expect=None, retries=3, quiet=False,
             on_progress=None):
    """下载一段回放，支持断点续传。

    与 lms_fetch.download 的关键差异：
      - 串行请求，失败后退避重试（绝不并发）
      - Range 起点以 Content-Range 为准（服务端会扩大）
      - 不做整体 sha256 比对（拿不到官方哈希），只做大小合理性检查

    返回 {ok, size, sha256, err, retried, resumed, declared, shortfall, note}
    declared  : 服务端声明的总字节数（Content-Range 总长，拿不到为 None）
    shortfall : 实得比声明少了多少比例（拿不到声明总长时为 None）
    note      : 人可读的补充说明，正常时为 None
    """
    tmp = path + ".part"
    last_err = None
    used_resume = False

    for attempt in range(retries):
        offset = 0
        if os.path.exists(tmp):
            offset = os.path.getsize(tmp)
            if expect and offset >= expect:
                offset = 0
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        try:
            r, start, status = _open_range(op, url, offset)

            # 服务端忽略了 Range：残片作废
            if offset and start != offset:
                if start == 0:
                    offset = 0
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

            total = None
            cr = r.headers.get("Content-Range")
            if cr:
                m = re.search(r"/(\d+)\s*$", cr)
                if m:
                    total = int(m.group(1))
            elif r.headers.get("Content-Length"):
                try:
                    total = int(r.headers["Content-Length"]) + offset
                except ValueError:
                    total = None

            ct = r.headers.get("Content-Type", "") or ""
            if "text/html" in ct:
                return {"ok": False, "err": "返回 HTML，非视频（登录态可能失效）"}

            h = hashlib.sha256()
            got = offset
            if offset:
                with open(tmp, "rb") as f:
                    while True:
                        blk = f.read(1048576)
                        if not blk:
                            break
                        h.update(blk)
                used_resume = True

            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            t0 = time.time()
            last_report = 0.0
            last_data = time.time()
            stalled = False

            with open(tmp, "ab" if offset else "wb") as f:
                while True:
                    if got > MAX_BYTES:
                        last_err = "超过 %dGB 上限" % (MAX_BYTES // 1073741824)
                        stalled = True
                        break
                    try:
                        chunk = r.read(CHUNK)
                    except Exception as e:
                        last_err = "读取中断: %s" % str(e)[:50]
                        stalled = True
                        break
                    if not chunk:
                        break                       # 自然 EOF
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    last_data = time.time()
                    if not quiet:
                        mb = got / 1048576.0
                        if mb - last_report >= 5:
                            last_report = mb
                            el = max(time.time() - t0, 0.001)
                            if on_progress:
                                on_progress(got, total)
                            elif os.environ.get("LMS_LIVE_PROGRESS") != "0":
                                pct = ("%5.1f%%" % (got * 100.0 / total)) if total else "  --- "
                                print("      %s %8.1fMB  %.2fMB/s"
                                      % (pct, mb, (got - offset) / el / 1048576.0),
                                      flush=True)
                    if time.time() - last_data > 120:
                        last_err = "卡死 120 秒无数据"
                        stalled = True
                        break

            if stalled:
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                return {"ok": False, "err": last_err, "kept_part": True}

            if got < 1024:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                last_err = "只有 %d 字节，疑似空响应" % got
                if attempt < retries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                return {"ok": False, "err": last_err}

            os.replace(tmp, path)
            # 短读判定：拿到声明总长时才算，不静默吞掉差异
            declared = None
            shortfall = None
            note = None
            if total and total > 0:
                declared = total
                shortfall = max(0.0, (total - got) / float(total))
                if shortfall > SHORT_TOLERANCE:
                    note = ("实得 %d 字节，服务端声明 %d 字节，少 %.1f%%"
                            % (got, total, shortfall * 100))
            return {"ok": True, "size": got, "sha256": h.hexdigest(),
                    "retried": attempt, "resumed": used_resume,
                    "declared": declared, "shortfall": shortfall, "note": note}

        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # 403 多半是并发或 token 限流。等一下再串行重试一次。
                last_err = "HTTP %d（token 限流或失效）" % e.code
            elif e.code == 404:
                return {"ok": False, "err": "HTTP 404（回放不存在）", "fatal": True}
            else:
                last_err = "HTTP %d" % e.code
        except Exception as e:
            last_err = "%s %s" % (type(e).__name__, str(e)[:60])

        if attempt < retries - 1:
            time.sleep(4 * (attempt + 1))

    return {"ok": False, "err": last_err or "未知错误", "kept_part": True}


def cam_label(camera_type):
    return {"encoder": "屏幕录制", "instructor": "教师机位"}.get(camera_type,
                                                                camera_type or "未知")


def safe_name(title, camera_type, stamp=None, ext=".mp4"):
    """给回放起个稳定的文件名。

    一台课有两路机位，且同一天的多个活动 title 完全相同 —— 两处都要区分开，
    否则会互相覆盖：
      - camera_type 区分机位
      - stamp（开始时间）区分同一天的上下场次
    """
    base = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (title or "回放").strip()).strip(" .")
    if len(base) > 80:
        base = base[:72] + "~" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:6]
    parts = [base]
    if stamp:
        parts.append(stamp)
    parts.append(camera_type or "unknown")
    return "-".join(parts) + ext
