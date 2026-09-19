#!/usr/bin/env python3
"""AhaMaker 企业自托管连接器（WeChat Official Account draft connector）。

企业把它部署在自己的服务器上：App 不直接持有 AppSecret，也不直接访问
微信公众平台；密钥、令牌与文章内容都留在企业网络内，公众号看到的出口
IP 就是这台服务器的固定 IP（把它加进公众号后台的 IP 白名单一次即可）。

仅依赖 Python 3.8+ 标准库，无需 pip 安装任何东西。

配置（环境变量）：
    AHAMAKER_CONNECTOR_TOKEN  访问令牌，App 侧以 Bearer 方式携带（必填）
    WXA_APPID                 公众号 AppID（必填）
    WXA_SECRET                公众号 AppSecret（必填，不要写进代码或镜像层）
    WXA_AUTHOR                默认作者名（可选）
    AHAMAKER_CONNECTOR_PORT   监听端口，默认 8807
    AHAMAKER_CONNECTOR_MAX_MB 请求体上限 MB，默认 40

接口：
    GET  /health  健康检查；带正确 Bearer 时返回 auth:true，用于 App 内「检查连接」
    POST /draft   保存草稿（需 Bearer）。body: {title, digest, html, author?}
                  返回 {media_id}；公众号错误按 {errcode, errmsg} 原样透传

自检：python3 wechat_connector.py --check
启动：AHAMAKER_CONNECTOR_TOKEN=... WXA_APPID=... WXA_SECRET=... python3 wechat_connector.py
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = "ahamaker-connector"
VERSION = 1
WECHAT_BASE = "https://api.weixin.qq.com"
MAX_IMAGES = 50

DATA_URL_PATTERN = re.compile(r"data:image/(?:png|jpe?g|gif);base64,[A-Za-z0-9+/=]+")

_token_cache = {"value": "", "app_id": "", "expires_at": 0.0}


class ApiError(Exception):
    def __init__(self, errcode, errmsg, status=502):
        super().__init__(errmsg)
        self.errcode = errcode
        self.errmsg = errmsg
        self.status = status


def config():
    return {
        "token": os.environ.get("AHAMAKER_CONNECTOR_TOKEN", "").strip(),
        "appid": os.environ.get("WXA_APPID", "").strip(),
        "secret": os.environ.get("WXA_SECRET", "").strip(),
        "author": os.environ.get("WXA_AUTHOR", "").strip(),
        "port": int(os.environ.get("AHAMAKER_CONNECTOR_PORT", "8807")),
        "max_body": int(os.environ.get("AHAMAKER_CONNECTOR_MAX_MB", "40")) * 1024 * 1024,
    }


def check_config():
    cfg = config()
    problems = []
    if len(cfg["token"]) < 16:
        problems.append("AHAMAKER_CONNECTOR_TOKEN 未设置或太短（至少 16 个字符）")
    if not cfg["appid"].startswith("wx"):
        problems.append("WXA_APPID 未设置或格式不对（应以 wx 开头）")
    if len(cfg["secret"]) < 16:
        problems.append("WXA_SECRET 未设置或太短")
    return cfg, problems


def wechat_request(path, params=None, method="GET", content_type=None, body=None):
    url = WECHAT_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, data=body, method=method)
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        payload = error.read()
    except urllib.error.URLError as error:
        raise ApiError(-1, "访问微信公众平台失败：%s" % error.reason, status=504)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(-2, "公众平台返回了无法解析的数据")
    code = data.get("errcode", 0)
    if code:
        raise ApiError(code, data.get("errmsg", ""), status=502)
    return data


def fetch_token(cfg, force=False):
    now = time.time()
    # 缓存与凭据配对，避免运行期改环境变量后复用旧 token。
    if (not force and _token_cache["value"] and _token_cache["app_id"] == cfg["appid"]
            and now < _token_cache["expires_at"]):
        return _token_cache["value"]
    data = wechat_request("/cgi-bin/token", params={
        "grant_type": "client_credential", "appid": cfg["appid"], "secret": cfg["secret"],
    })
    value = data.get("access_token", "")
    if not value:
        raise ApiError(-3, "公众平台未返回 access_token")
    _token_cache.update(value=value, app_id=cfg["appid"],
                        expires_at=now + max(60, data.get("expires_in", 7200) - 300))
    return value


def extract_data_urls(html):
    seen, results = set(), []
    for match in DATA_URL_PATTERN.findall(html or ""):
        if match not in seen:
            seen.add(match)
            results.append(match)
    return results


def decode_image(source):
    header, _, payload = source.partition(",")
    if not payload or not header.endswith(";base64"):
        raise ApiError(40007, "文章中有一张图片无法读取", status=400)
    try:
        data = base64.b64decode(payload)
    except (ValueError, TypeError):
        raise ApiError(40007, "文章中有一张图片 base64 无法解码", status=400)
    if not data:
        raise ApiError(40007, "文章中有一张图片为空", status=400)
    if "image/png" in header:
        mime, ext = "image/png", "png"
    elif "image/gif" in header:
        mime, ext = "image/gif", "gif"
    else:
        mime, ext = "image/jpeg", "jpg"
    return data, mime, ext


def multipart(field, filename, mime, data):
    boundary = "AhaMakerConnector-%d" % time.monotonic_ns()
    body = b""
    body += ("--%s\r\n" % boundary).encode()
    body += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (field, filename)).encode()
    body += ("Content-Type: %s\r\n\r\n" % mime).encode()
    body += data + ("\r\n--%s--\r\n" % boundary).encode()
    return boundary, body


def wechat_request_with_retry(cfg, path, params=None, method="GET", content_type=None, body=None):
    """带令牌失效自愈的公众平台调用：任何人在别处刷新 access_token（新 token 生效
    后旧 token 作废）都会让本进程缓存的令牌变成 40001/42001/40014，此时强制刷新
    缓存并重放一次，而不是让分发失败直到缓存自然过期（最长 2 小时）。"""
    token = fetch_token(cfg, force=False)
    try:
        return wechat_request(path, params=dict(params or {}, access_token=token),
                              method=method, content_type=content_type, body=body)
    except ApiError as error:
        if error.errcode not in (40001, 42001, 40014):
            raise
        token = fetch_token(cfg, force=True)
        return wechat_request(path, params=dict(params or {}, access_token=token),
                              method=method, content_type=content_type, body=body)


def save_draft(cfg, payload):
    title = str(payload.get("title") or "未命名文章").strip()[:64]
    digest = str(payload.get("digest") or "").strip()[:120]
    author = str(payload.get("author") or cfg["author"]).strip()[:16]
    html = payload.get("html") or ""
    if not html.strip():
        raise ApiError(-4, "文章内容为空，无法分发", status=400)

    images = extract_data_urls(html)[:MAX_IMAGES]
    if not images:
        raise ApiError(-5, "公众号草稿需要封面：文章中至少插入一张图片", status=400)

    cover_data, cover_mime, cover_ext = decode_image(images[0])
    if len(cover_data) > 10 * 1024 * 1024:
        raise ApiError(40007, "封面超过 10 MB，请压缩后重试", status=400)
    boundary, body = multipart("media", "cover.%s" % cover_ext, cover_mime, cover_data)
    cover = wechat_request_with_retry(cfg, "/cgi-bin/material/add_material",
                                      method="POST", content_type="multipart/form-data; boundary=%s" % boundary, body=body)
    cover_media_id = cover.get("media_id", "")
    if not cover_media_id:
        raise ApiError(-6, "封面上传失败：公众平台未返回 media_id")

    # 封面同样会以 data URI 形式留在正文开头（App 会在正文最前插入封面图），
    # add_draft 的 content 有长度上限，base64 数据留在里面会被微信以
    # "content size out of limit" 拒绝。所以封面再走一次 uploadimg 换取图床 URL 后替换。
    content = html
    boundary, body = multipart("media", "cover_uploadimg.%s" % cover_ext, cover_mime, cover_data)
    try:
        hosted_cover = wechat_request_with_retry(cfg, "/cgi-bin/media/uploadimg",
                                                 method="POST", content_type="multipart/form-data; boundary=%s" % boundary, body=body)
        cover_url = hosted_cover.get("url", "")
        content = content.replace(images[0], cover_url if str(cover_url).startswith(("http://", "https://")) else "")
    except ApiError:
        content = content.replace(images[0], "")  # 图床失败时也要把 base64 移出正文

    for index, source in enumerate(images[1:], start=1):
        image_data, image_mime, image_ext = decode_image(source)
        if len(image_data) > 1 * 1024 * 1024:
            raise ApiError(40007, "第 %d 张正文图片超过 1 MB，公众号正文图床不接收" % index, status=400)
        boundary, body = multipart("media", "img%d.%s" % (index, image_ext), image_mime, image_data)
        hosted = wechat_request_with_retry(cfg, "/cgi-bin/media/uploadimg",
                                           method="POST", content_type="multipart/form-data; boundary=%s" % boundary, body=body)
        url = hosted.get("url", "")
        if not str(url).startswith(("http://", "https://")):
            raise ApiError(-7, "第 %d 张正文图片上传失败：未返回图床地址" % index)
        content = content.replace(source, url)

    article = {
        "article_type": "news", "title": title, "digest": digest, "content": content,
        "thumb_media_id": cover_media_id, "need_open_comment": 0, "only_fans_can_comment": 0,
    }
    if author:
        article["author"] = author
    body = json.dumps({"articles": [article]}, ensure_ascii=False).encode("utf-8")
    created = wechat_request_with_retry(cfg, "/cgi-bin/draft/add",
                                        method="POST", content_type="application/json", body=body)
    media_id = created.get("media_id", "")
    if not media_id:
        raise ApiError(-8, "草稿创建失败：公众平台未返回 media_id")
    return {"media_id": media_id, "saved": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "AhaMakerConnector/%d" % VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    def _send(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        cfg = config()
        header = self.headers.get("Authorization", "")
        return bool(cfg["token"]) and header == "Bearer " + cfg["token"]

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path != "/health":
            return self._send(404, {"ok": False, "error": "not found"})
        if not config()["token"]:
            return self._send(500, {"ok": False, "error": "服务端未配置 AHAMAKER_CONNECTOR_TOKEN"})
        authed = self._authorized()
        return self._send(200, {"ok": True, "service": SERVICE, "version": VERSION, "auth": authed})

    def do_POST(self):
        cfg = config()
        if not cfg["token"]:
            return self._send(500, {"ok": False, "error": "服务端未配置 AHAMAKER_CONNECTOR_TOKEN"})
        path = urllib.parse.urlparse(self.path).path
        if path != "/draft":
            return self._send(404, {"ok": False, "error": "not found"})
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"ok": False, "error": "Content-Length 不合法"})
        if length <= 0 or length > cfg["max_body"]:
            return self._send(400, {"ok": False, "error": "请求体为空或超过上限"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"ok": False, "error": "请求体不是合法 JSON"})
        if not isinstance(payload, dict):
            return self._send(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
        try:
            return self._send(200, save_draft(cfg, payload))
        except ApiError as error:
            return self._send(error.status, {"ok": False, "errcode": error.errcode, "errmsg": error.errmsg})


def main():
    if "--check" in sys.argv:
        cfg, problems = check_config()
        if problems:
            print("配置未通过：")
            for problem in problems:
                print("  -", problem)
            return 1
        print("配置 OK：端口 %d，AppID %s…" % (cfg["port"], cfg["appid"][:6]))
        return 0
    cfg, problems = check_config()
    if problems:
        for problem in problems:
            print("配置错误：" + problem, file=sys.stderr)
        return 1
    server = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), Handler)
    print("%s v%d listening on 0.0.0.0:%d (appid %s…)" % (SERVICE, VERSION, cfg["port"], cfg["appid"][:6]))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
