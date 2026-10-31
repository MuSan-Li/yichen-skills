#!/usr/bin/env python3
"""
抖音视频下载器 - 使用 Playwright 拦截 Network 响应获取无水印直链

依赖安装:
    pip install playwright requests
    python3 -m playwright install chromium
    # 缺少 Chromium 时默认报错并给出安装命令；只有显式 --install-chromium 才会自动安装（带超时）。

只接受视频详情链接：/video/<id>、带 modal_id 的链接，或 v.douyin.com 短链（先跟随跳转再校验）。
视频先写入 .part，校验 Content-Length 后才改为正式文件名。

用法:
    python3 douyin_download.py "<抖音链接>" [输出路径]
    python3 douyin_download.py "https://www.douyin.com/video/7611845735025364265"
    python3 douyin_download.py "<抖音链接>" "~/Downloads/my_video.mp4"
    python3 douyin_download.py "<抖音链接>" --metadata-only
"""

import argparse
import hashlib
import json
import sys
import os
import re
import subprocess
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:
    PlaywrightError = Exception
    sync_playwright = None


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MISSING_BROWSER_HINTS = (
    "Executable doesn't exist",
    "playwright install",
    "Looks like Playwright was just installed",
)
INSTALL_COMMAND = "python3 -m playwright install chromium"
DEFAULT_INSTALL_TIMEOUT = 600
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SHORT_LINK_HOSTS = {"v.douyin.com"}
VIDEO_PATH_RE = re.compile(r"/video/(?P<id>\d{6,30})/?")
# v.douyin.com 短链的常见落点；只作为跳转目标接受，不作为直接输入。
SHARE_VIDEO_PATH_RE = re.compile(r"/share/video/(?P<id>\d{6,30})/?")
VIDEO_ID_RE = re.compile(r"\d{6,30}")
URL_IN_TEXT_RE = re.compile(r"(?i)\b(https?)://([^/\s?#'\"<>]+)[^\s'\"<>]*")
DETAIL_URL_HINT = (
    "只接受抖音视频详情链接：/video/<id>、带 modal_id 的链接或 v.douyin.com 短链；"
    "不支持搜索、用户主页、推荐页或其他页面"
)


class ChromiumMissingError(RuntimeError):
    """Playwright 缺少 Chromium，且用户没有显式允许自动安装。"""


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_douyin_host(host: str) -> bool:
    return host == "douyin.com" or host.endswith(".douyin.com")


def is_iesdouyin_host(host: str) -> bool:
    return host == "iesdouyin.com" or host.endswith(".iesdouyin.com")


def _plain_https(url: str) -> bool:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and port in (None, 443)
    )


def sanitize_error_text(text: str) -> str:
    """错误输出只保留 URL 的协议与主机，去掉带签名的路径和查询。"""
    return URL_IN_TEXT_RE.sub(lambda match: f"{match.group(1)}://{match.group(2)}/…", text or "")


def validate_douyin_url(url: str) -> str:
    """只接受抖音 HTTPS 链接，不把本执行器变成通用浏览器。"""
    if not _plain_https(url) or not is_douyin_host(_host(url)):
        raise ValueError("只接受 https://*.douyin.com 的已知视频链接")
    return url


def extract_video_id(url: str) -> Optional[str]:
    """只从视频详情形态中提取 ID：/video/<id>，或单个 modal_id / resource_id 查询参数。"""
    parsed = urlparse(url)
    host = _host(url)
    path = re.sub(r"/+", "/", parsed.path or "/")
    if is_douyin_host(host):
        match = VIDEO_PATH_RE.fullmatch(path)
        if match:
            return match.group("id")
        query = parse_qs(parsed.query)
        for key in ("modal_id", "resource_id"):
            values = query.get(key, [])
            if len(values) == 1 and VIDEO_ID_RE.fullmatch(values[0]):
                return values[0]
    elif is_iesdouyin_host(host):
        match = SHARE_VIDEO_PATH_RE.fullmatch(path)
        if match:
            return match.group("id")
    return None


def normalize_url(url: str) -> str:
    """将受支持的抖音视频链接转换为标准详情页 URL；其他页面一律拒绝。"""
    validate_douyin_url(url)
    if _host(url) in SHORT_LINK_HOSTS:
        raise ValueError("v.douyin.com 短链必须先跟随跳转，再校验最终视频详情链接")
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError(DETAIL_URL_HINT)
    return f"https://www.douyin.com/video/{video_id}"


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def resolve_short_link(url: str) -> str:
    """匿名跟随 v.douyin.com 跳转，直到得到视频详情 URL；不读取页面正文。"""
    current = url
    for _ in range(MAX_REDIRECTS):
        response = requests.get(
            current,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=False,
            stream=True,
            timeout=30,
        )
        try:
            status = response.status_code
            location = response.headers.get("Location") or ""
        finally:
            _close(response)
        if status not in REDIRECT_STATUSES or not location:
            raise ValueError(f"抖音短链没有返回跳转（HTTP {status}），无法确认目标视频")
        target = urljoin(current, location)
        parsed = urlparse(target)
        host = _host(target)
        if not (is_douyin_host(host) or is_iesdouyin_host(host)):
            raise ValueError(f"短链跳转到抖音之外的站点，已停止: {host or '?'}")
        if parsed.scheme == "http":
            target = urlunparse(parsed._replace(scheme="https"))
        if not _plain_https(target):
            raise ValueError(f"短链跳转地址不是普通 HTTPS 链接，已停止: {host}")
        if extract_video_id(target):
            return target
        if host not in SHORT_LINK_HOSTS:
            raise ValueError(f"短链跳转目标不是视频详情页: {host}{parsed.path}；{DETAIL_URL_HINT}")
        current = target
    raise ValueError("抖音短链跳转次数过多，已停止")


def resolve_video_target(url: str) -> Tuple[str, str]:
    """返回 (标准详情页 URL, 视频 ID)。短链先跳转，再按详情形态校验。"""
    validate_douyin_url(url)
    target = resolve_short_link(url) if _host(url) in SHORT_LINK_HOSTS else url
    video_id = extract_video_id(target)
    if not video_id:
        raise ValueError(DETAIL_URL_HINT)
    return f"https://www.douyin.com/video/{video_id}", video_id


def install_playwright_chromium(timeout: int) -> None:
    """仅在用户显式传入 --install-chromium 时调用；带超时，失败给出手动命令。"""
    print(f"按 --install-chromium 安装 Playwright Chromium（最长 {timeout} 秒）...")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"安装 Chromium 超时（{timeout} 秒）；请手动运行: {INSTALL_COMMAND}") from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError(f"安装 Chromium 失败；请手动运行: {INSTALL_COMMAND}") from exc


def get_best_video_url(aweme_data: dict) -> Optional[str]:
    """从 aweme 数据里按优先级提取可下载视频地址。"""
    video = aweme_data.get('video', {}) or {}
    candidates = [
        video.get('play_addr', {}),
        video.get('play_addr_h264', {}),
        video.get('download_addr', {}),
    ]

    for item in candidates:
        url_list = item.get('url_list') or []
        if url_list:
            return url_list[0]
    return None


def fetch_video_info(
    url: str,
    timeout: int = 60,
    *,
    install_chromium: bool = False,
    install_timeout: int = DEFAULT_INSTALL_TIMEOUT,
):
    """
    使用 Playwright 拦截 aweme/detail API 获取视频信息
    返回包含 video_url, title, author 等信息的字典
    """
    normalized, video_id = resolve_video_target(url)
    if sync_playwright is None:
        raise RuntimeError(
            "缺少 playwright；请先运行 pip install playwright && "
            f"{INSTALL_COMMAND}"
        )
    try:
        return _fetch_video_info_once(normalized, video_id, timeout=timeout)
    except PlaywrightError as exc:
        if not any(hint in str(exc) for hint in MISSING_BROWSER_HINTS):
            raise
        if not install_chromium:
            raise ChromiumMissingError(
                "缺少 Playwright Chromium，未自动安装。请由用户确认后运行: "
                f"{INSTALL_COMMAND}；或显式加 --install-chromium 让本脚本安装"
                f"（最长 {install_timeout} 秒）"
            ) from exc
        install_playwright_chromium(install_timeout)
        return _fetch_video_info_once(normalized, video_id, timeout=timeout)


def is_detail_api_response(url: str) -> bool:
    """按主机名与路径判断是否为抖音详情接口，不用子串匹配。"""
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and is_douyin_host(_host(url))
        and re.search(r"/aweme/detail/?$", parsed.path or "") is not None
    )


def _fetch_video_info_once(normalized: str, video_id: str, timeout: int = 60):
    """单次打开页面并拦截详情接口；只接受 aweme_id 与请求 ID 一致的响应。"""
    video_url = None
    aweme_data = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale='zh-CN',
            )
            page = context.new_page()

            def handle_response(response):
                nonlocal video_url, aweme_data
                if aweme_data or not is_detail_api_response(response.url):
                    return
                try:
                    body = response.json()
                except Exception:
                    return
                detail = body.get('aweme_detail') if isinstance(body, dict) else None
                # 页面还会请求推荐视频的详情；只认与链接 ID 一致的那一条。
                if not isinstance(detail, dict) or str(detail.get('aweme_id') or '') != video_id:
                    return
                aweme_data = detail
                video_url = get_best_video_url(detail)

            page.on('response', handle_response)

            print(f"正在访问: {normalized}")
            try:
                page.goto(normalized, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                print(f"页面加载提示: {sanitize_error_text(str(e))}")

            deadline = time.time() + timeout
            while time.time() < deadline and not aweme_data:
                page.wait_for_timeout(1000)
        finally:
            browser.close()

    if not aweme_data:
        raise ValueError("无法获取目标视频数据（没有收到 aweme_id 与链接一致的详情响应），请检查链接是否有效")

    if not video_url:
        video_url = get_best_video_url(aweme_data)

    return {
        'video_url': video_url,
        'title': aweme_data.get('desc', ''),
        'author': aweme_data.get('author', {}).get('nickname', ''),
        'aweme_id': str(aweme_data.get('aweme_id') or video_id),
        'aweme_data': aweme_data,
        'canonical_url': normalized,
    }


def build_metadata(info: dict, source_url: str, *, download_status: str = 'pending') -> dict:
    """生成不包含临时直链的精简元数据；视频文件信息在下载完成后补写。"""
    aweme_data = info.get('aweme_data', {}) or {}
    author = aweme_data.get('author', {}) or {}
    statistics = aweme_data.get('statistics', {}) or {}
    video = aweme_data.get('video', {}) or {}

    return {
        'source_url': info.get('canonical_url') or source_url,
        'fetched_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'aweme_id': info.get('aweme_id') or aweme_data.get('aweme_id', ''),
        'title': info.get('title') or aweme_data.get('desc', ''),
        'author': info.get('author') or author.get('nickname', ''),
        'author_id': author.get('uid', ''),
        'create_time': aweme_data.get('create_time'),
        'duration_ms': video.get('duration'),
        'statistics': statistics,
        'download_status': download_status,
        'video_path': None,
        'size_bytes': None,
        'sha256': None,
    }


def write_metadata(info: dict, source_url: str, output_path: str, *, download_status: str = 'pending') -> Path:
    metadata_path = Path(f"{output_path}.metadata.json")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = build_metadata(info, source_url, download_status=download_status)
    with metadata_path.open("x", encoding="utf-8") as file:
        file.write(json.dumps(metadata, ensure_ascii=False, indent=2))
    return metadata_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def update_metadata(metadata_path: Path, updates: dict) -> None:
    """只更新本次运行刚创建的 metadata：先排他写临时文件，再原子替换。"""
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    metadata.update(updates)
    temporary = metadata_path.with_name(f"{metadata_path.name}.tmp")
    with temporary.open('x', encoding='utf-8') as file:
        file.write(json.dumps(metadata, ensure_ascii=False, indent=2))
    os.replace(temporary, metadata_path)


def choose_unique_output_path(output_path: str) -> Path:
    """返回不与视频或相邻元数据冲突的输出路径（与其他执行器一致，首个重跑为 -run-2）。"""
    requested = Path(output_path).expanduser()
    candidate = requested
    counter = 2
    while (
        candidate.exists()
        or Path(f"{candidate}.metadata.json").exists()
        or Path(f"{candidate}.part").exists()
    ):
        candidate = requested.with_name(
            f"{requested.stem}-run-{counter}{requested.suffix}"
        )
        counter += 1
    return candidate


def _declared_length(response: Any) -> int:
    """无压缩编码时返回 Content-Length；缺失、非法或有编码时返回 0（只做非空校验）。"""
    headers = getattr(response, 'headers', None) or {}
    encoding = str(headers.get('Content-Encoding') or '').strip().lower()
    raw = str(headers.get('Content-Length') or '').strip()
    if encoding not in {'', 'identity'} or not raw.isdigit():
        return 0
    return int(raw)


def download_video(video_url: str, output_path: str, referer: str = 'https://www.douyin.com/') -> str:
    """先写 <name>.part，长度校验通过后才改成正式文件名。"""
    if not video_url:
        raise ValueError("没有拿到可下载的视频直链")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + '.part')

    headers = {
        'User-Agent': USER_AGENT,
        'Referer': referer,
    }

    response = requests.get(video_url, headers=headers, stream=True, timeout=60)
    downloaded = 0
    try:
        response.raise_for_status()

        total = _declared_length(response)
        next_percent = 0
        next_mb_report = 5

        with open(partial, 'xb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        percent = downloaded * 100 // total
                        if percent >= next_percent or downloaded == total:
                            print(f"下载进度: {percent}% ({downloaded//1024//1024}MB / {total//1024//1024}MB)")
                            next_percent += 10
                    else:
                        downloaded_mb = downloaded // 1024 // 1024
                        if downloaded_mb >= next_mb_report:
                            print(f"已下载: {downloaded_mb}MB")
                            next_mb_report += 5
    finally:
        _close(response)

    if downloaded <= 0:
        raise ValueError(f"下载结果为空；未完成的临时文件已保留: {partial}")
    if total and downloaded != total:
        raise ValueError(
            f"下载不完整（声明 {total} 字节，实际 {downloaded} 字节）；未完成的临时文件已保留: {partial}"
        )
    if output.exists():
        raise FileExistsError(f"拒绝覆盖既有文件: {output}")
    partial.rename(output)
    return str(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description="抖音视频下载器")
    parser.add_argument("url", help="抖音视频详情链接或 v.douyin.com 短链")
    parser.add_argument("output_path", nargs="?", help="输出 MP4 路径，默认 ~/Downloads/douyin_<video_id>.mp4")
    parser.add_argument("--metadata-only", action="store_true", help="只抓取并保存元数据，不下载视频")
    parser.add_argument("--timeout", type=int, default=60, help="等待 aweme/detail 响应的秒数，默认 60")
    parser.add_argument(
        "--install-chromium",
        action="store_true",
        help="缺少 Playwright Chromium 时允许本脚本自动安装；默认只报错并给出安装命令",
    )
    parser.add_argument(
        "--install-timeout",
        type=int,
        default=DEFAULT_INSTALL_TIMEOUT,
        help=f"自动安装 Chromium 的最长秒数，默认 {DEFAULT_INSTALL_TIMEOUT}",
    )
    args = parser.parse_args(argv)
    if args.install_timeout < 1:
        parser.error("--install-timeout 必须是正整数")

    url = args.url
    output_path = args.output_path

    print("=" * 50)
    print("抖音视频下载器")
    print("=" * 50)

    try:
        info = fetch_video_info(
            url,
            timeout=args.timeout,
            install_chromium=args.install_chromium,
            install_timeout=args.install_timeout,
        )
        video_url = info['video_url']
        title = info['title']
        author = info['author']
        aweme_id = info['aweme_id']

        print(f"\n视频标题: {title or '(无标题)'}")
        print(f"作者: {author}")
        print(f"视频ID: {aweme_id}")

        if not output_path:
            downloads_dir = Path.home() / 'Downloads'
            downloads_dir.mkdir(exist_ok=True)
            output_path = str(downloads_dir / f"douyin_{aweme_id}.mp4")

        requested_output = Path(output_path).expanduser()
        unique_output = choose_unique_output_path(str(requested_output))
        if unique_output != requested_output:
            print(f"目标已存在，改用不冲突路径: {unique_output}")
        output_path = str(unique_output)

        metadata_path = write_metadata(
            info,
            url,
            output_path,
            download_status='metadata_only' if args.metadata_only else 'pending',
        )
        print(f"元数据: {metadata_path}")

        if args.metadata_only:
            print("已按 --metadata-only 跳过视频下载")
            return

        print("\n开始下载视频...")
        try:
            result_path = download_video(video_url, output_path)
        except Exception as e:
            message = sanitize_error_text(str(e))
            try:
                update_metadata(metadata_path, {'download_status': 'failed', 'download_error': message})
            except (OSError, ValueError):
                pass
            print(f"\n下载失败: {message}")
            print("失败项:")
            print(f"- 视频 {aweme_id}: 未生成 {output_path}（元数据已保存: {metadata_path}）")
            sys.exit(1)

        video_file = Path(result_path)
        size_bytes = video_file.stat().st_size
        update_metadata(
            metadata_path,
            {
                'download_status': 'success',
                'video_path': str(video_file),
                'size_bytes': size_bytes,
                'sha256': sha256_file(video_file),
            },
        )
        print("\n下载完成")
        print(f"   保存路径: {result_path}")
        print(f"   文件大小: {size_bytes / 1024 / 1024:.2f} MB")

    except Exception as e:
        print(f"\n下载失败: {sanitize_error_text(str(e))}")
        sys.exit(1)


if __name__ == '__main__':
    main()
