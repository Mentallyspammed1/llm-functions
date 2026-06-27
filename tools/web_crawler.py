#!/usr/bin/env python3
# @describe Web crawler that parses HTML, extracts links, and recursively follows them to search for keywords or download images/thumbnails.
# @option --start-url! <TEXT>                      Starting URL to crawl
# @option --max-depth <NUM>                        Maximum crawl depth (0 = start page only, default: 1)
# @option --max-pages <NUM>                        Maximum total pages to crawl (default: 10)
# @option --keyword <TEXT>                         Search for a specific keyword in the page text
# @flag   --domain-restrict                        Restrict crawl to links on the same domain as start-url (default: True)
# @flag   --download-images                        Enable automatic downloading of images found in crawled pages
# @option --media-dir <PATH>                       Directory to save downloaded images (default: cache/crawled_media/)
# @flag   --generate-thumbnails                    Generate thumbnails for downloaded images (requires PIL/Pillow)
# @option --thumb-dir <PATH>                       Directory to save thumbnails (default: cache/crawled_thumbs/)
# @option --thumb-width <NUM>                      Thumbnail width (default: 150)
# @flag   --verbose                                Enable verbose log messages
"""
web_crawler.py - HTML parsing, crawling, and link following tool.
"""

import os
import json
import sys
import argparse
import urllib.parse
import urllib.request
import re
import logging
import requests
from html.parser import HTMLParser
from typing import List, Dict, Any, Set, Tuple

class LinkExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: Set[str] = set()
        self.image_links: Set[str] = set()
        self.text_parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for attr, val in attrs:
                if attr == "href" and val:
                    abs_url = urllib.parse.urljoin(self.base_url, val.strip())
                    abs_url = abs_url.split("#")[0]
                    if abs_url.startswith(("http://", "https://")):
                        self.links.add(abs_url)
        elif tag == "img":
            for attr, val in attrs:
                if attr == "src" and val:
                    abs_url = urllib.parse.urljoin(self.base_url, val.strip())
                    abs_url = abs_url.split("#")[0]
                    if abs_url.startswith(("http://", "https://")):
                        self.image_links.add(abs_url)

    def handle_data(self, data):
        self.text_parts.append(data)

    def get_text(self) -> str:
        return " ".join(self.text_parts)

def fetch_and_parse(url: str, verbose: bool = False) -> Tuple[str, str, Set[str], Set[str]]:
    """Fetch URL and return status, page_text, extracted links, and image links."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        if verbose:
            print(f"Crawling: {url}", file=sys.stderr)
        with urllib.request.urlopen(req, timeout=15) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                return "skipped_non_html", "", set(), set()
            
            raw_bytes = response.read()
            try:
                html = raw_bytes.decode("utf-8", errors="replace")
            except Exception:
                html = raw_bytes.decode("latin-1")
                
            parser = LinkExtractor(url)
            parser.feed(html)
            
            page_text = re.sub(r'\s+', ' ', parser.get_text()).strip()
            return "ok", page_text, parser.links, parser.image_links
    except Exception as e:
        if verbose:
            print(f"Error fetching {url}: {e}", file=sys.stderr)
        return f"error: {str(e)}", "", set(), set()

def download_image_file(url: str, download_dir: str) -> str:
    """Download single image file into download_dir."""
    os.makedirs(download_dir, exist_ok=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        parsed_url = urllib.parse.urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename or "." not in filename:
            filename = f"image_{hash(url) & 0xffffffff}.jpg"
        # Sanitize filename
        filename = "".join(c for c in filename if c.isalnum() or c in "._-")
        output_path = os.path.join(download_dir, filename)
        
        response = requests.get(url, headers=headers, timeout=20, stream=True)
        response.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return os.path.abspath(output_path)
    except Exception as e:
        logging.warning(f"Failed to download image from {url}: {e}")
        return None

def make_thumbnail(image_path: str, thumb_dir: str, width: int = 150) -> str:
    """Generate image thumbnail using Pillow."""
    os.makedirs(thumb_dir, exist_ok=True)
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            ratio = width / float(img.size[0])
            height = int(float(img.size[1]) * float(ratio))
            img.thumbnail((width, height), Image.Resampling.LANCZOS)
            
            filename = "thumb_" + os.path.basename(image_path)
            thumb_path = os.path.join(thumb_dir, filename)
            img.save(thumb_path)
            return os.path.abspath(thumb_path)
    except Exception as e:
        logging.warning(f"Failed to generate thumbnail for {image_path}: {e}")
        return None

def run(
    start_url: str,
    max_depth: int = 1,
    max_pages: int = 10,
    keyword: str = None,
    domain_restrict: bool = True,
    download_images: bool = False,
    media_dir: str = None,
    generate_thumbnails: bool = False,
    thumb_dir: str = None,
    thumb_width: int = 150,
    verbose: bool = False,
) -> dict:
    parsed_start = urllib.parse.urlparse(start_url)
    start_domain = parsed_start.netloc
    
    queue: List[Tuple[str, int]] = [(start_url, 0)]  # Queue of (url, depth)
    visited: Set[str] = set()
    crawled_pages: List[dict] = []
    
    # Resolve directories
    if download_images and not media_dir:
        media_dir = os.path.join(os.getcwd(), "cache", "crawled_media")
    if generate_thumbnails and not thumb_dir:
        thumb_dir = os.path.join(os.getcwd(), "cache", "crawled_thumbs")
        
    keyword_lower = keyword.lower() if keyword else None
    
    while queue and len(crawled_pages) < max_pages:
        url, depth = queue.pop(0)
        
        norm_url = url.rstrip("/")
        if norm_url in visited:
            continue
        visited.add(norm_url)
        
        if domain_restrict:
            parsed_curr = urllib.parse.urlparse(url)
            if parsed_curr.netloc != start_domain:
                continue
                
        status, page_text, links, image_links = fetch_and_parse(url, verbose)
        
        page_info = {
            "url": url,
            "depth": depth,
            "status": status,
            "keyword_found": False,
            "snippet": None,
            "links_found_count": len(links),
            "images_found_count": len(image_links),
            "downloads": []
        }
        
        if status == "ok":
            if keyword_lower:
                idx = page_text.lower().find(keyword_lower)
                if idx != -1:
                    page_info["keyword_found"] = True
                    start_idx = max(0, idx - 100)
                    end_idx = min(len(page_text), idx + len(keyword) + 100)
                    page_info["snippet"] = "..." + page_text[start_idx:end_idx].strip() + "..."
            
            # Download images if requested
            if download_images and image_links:
                for img_url in image_links:
                    saved_path = download_image_file(img_url, media_dir)
                    if saved_path:
                        download_meta = {
                            "url": img_url,
                            "saved_path": saved_path,
                            "thumbnail_path": None
                        }
                        if generate_thumbnails:
                            thumb_path = make_thumbnail(saved_path, thumb_dir, thumb_width)
                            download_meta["thumbnail_path"] = thumb_path
                        page_info["downloads"].append(download_meta)
                        
            crawled_pages.append(page_info)
            
            if depth < max_depth:
                for link in links:
                    norm_link = link.rstrip("/")
                    if norm_link not in visited:
                        queue.append((link, depth + 1))
        else:
            page_info["error"] = status
            crawled_pages.append(page_info)
            
    return {
        "success": True,
        "start_url": start_url,
        "domain_restricted": domain_restrict,
        "total_crawled_count": len(crawled_pages),
        "pages": crawled_pages
    }

if __name__ == "__main__":
    # 1. Parse JSON input if passed by aichat's tool dispatcher
    if len(sys.argv) > 1 and (sys.argv[1].startswith("{") or sys.argv[1].startswith("[")):
        try:
            kwargs = json.loads(sys.argv[1])
            start_val = kwargs.get("start_url")
            depth_val = kwargs.get("max_depth", 1)
            pages_val = kwargs.get("max_pages", 10)
            keyword_val = kwargs.get("keyword")
            restrict_val = kwargs.get("domain_restrict", True)
            dl_val = kwargs.get("download_images", False)
            media_val = kwargs.get("media_dir")
            genthumb_val = kwargs.get("generate_thumbnails", False)
            thumbdir_val = kwargs.get("thumb_dir")
            width_val = kwargs.get("thumb_width", 150)
            verb_val = kwargs.get("verbose", False)
            
            if not start_val:
                print(json.dumps({"success": False, "error": "start_url is required"}))
                sys.exit(1)
            print(json.dumps(run(
                start_url=start_val,
                max_depth=depth_val,
                max_pages=pages_val,
                keyword=keyword_val,
                domain_restrict=restrict_val,
                download_images=dl_val,
                media_dir=media_val,
                generate_thumbnails=genthumb_val,
                thumb_dir=thumbdir_val,
                thumb_width=width_val,
                verbose=verb_val
            ), indent=2))
            sys.exit(0)
        except Exception as err:
            print(json.dumps({"success": False, "error": f"JSON argument parse error: {err}"}))
            sys.exit(1)

    # 2. Fallback to standard CLI arguments
    parser = argparse.ArgumentParser(description="Web crawler and link follower")
    parser.add_argument("--start-url", required=True)
    parser.add_argument("--max-depth", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--keyword")
    parser.add_argument("--domain-restrict", action="store_true", default=True)
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument("--media-dir")
    parser.add_argument("--generate-thumbnails", action="store_true")
    parser.add_argument("--thumb-dir")
    parser.add_argument("--thumb-width", type=int, default=150)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(
        start_url=args.start_url,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        keyword=args.keyword,
        domain_restrict=args.domain_restrict,
        download_images=args.download_images,
        media_dir=args.media_dir,
        generate_thumbnails=args.generate_thumbnails,
        thumb_dir=args.thumb_dir,
        thumb_width=args.thumb_width,
        verbose=args.verbose
    ), indent=2))
