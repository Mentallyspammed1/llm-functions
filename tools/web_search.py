#!/usr/bin/env python3
import argparse, csv, json, sys, time
from typing import List, Optional, Dict, Any

def retry(max_retries: int = 2, base_delay: float = 1.0):
    """Decorator that retries a function with exponential back‑off."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_retries:
                        raise
                    time.sleep(delay)
                    delay *= 2
        return wrapper
    return decorator

@retry(max_retries=2, base_delay=1.0)
def _fetch_search_results(payload: dict) -> List[dict]:
    """
    Call the ydc_search tool (which internally uses the You.com API)
    and return a list of result dictionaries.
    """
    # Extract parameters needed by ydc_search
    query = payload["query"]
    limit = payload.get("limit", 10)
    include_domains = payload.get("include_domains")
    exclude_domains = payload.get("exclude_domains")
    # Import the ydc_search module and invoke its `search_ydc` function
    from ydc_search import search_ydc
    return search_ydc(
        query=query,
        count=limit,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
    )

def run(
    query: str,
    limit: int = 10,
    *,
    include_domains: Optional[str] = None,
    exclude_domains: Optional[str] = None,
    date_filter: Optional[str] = None,
    site_filter: Optional[str] = None,
    file_type: Optional[str] = None,
    lang: Optional[str] = None,
    safe: bool = False,
    export_format: str = "table",
    timeout: int = 15,
    max_retries: int = 2,
) -> List[dict]:
    """
    Perform a web search with retries, filters, and export options.
    Returns a list of result dictionaries.
    """
    # Build a payload that contains the parameters we want to forward
    payload = {
        "query": query,
        "limit": limit,
        "include_domains": include_domains,
        "exclude_domains": exclude_domains,
        # The remaining filters are not part of ydc_search’s signature,
        # but we keep them in the payload for future extensibility.
        "date_filter": date_filter,
        "site_filter": site_filter,
        "file_type": file_type,
        "lang": lang,
        "safe": safe,
    }
    results = _fetch_search_results(payload)

    # Deduplicate by URL
    seen = set()
    uniq: List[dict] = []
    for r in results:
        url = r.get("url")
        if url and url not in seen:
            seen.add(url)
            uniq.append(r)
    results = uniq

    # Output handling
    if sys.stdout.isatty():
        _print_pretty_table(results, export_format)
    else:
        if export_format == "json":
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            _print_pretty_table(results, export_format, json_mode=True)
    return results

def _print_pretty_table(results: List[dict], fmt: str, json_mode: bool = False) -> None:
    """Render ``results`` according to ``fmt``."""
    if json_mode or fmt == "json":
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return
    if fmt == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=["title", "url", "snippet"])
        writer.writeheader()
        for r in results:
            writer.writerow(r)
        return
    # Default simple column view
    if not results:
        print("No results found.")
        return
    title_w = max(len("Title"), max(len(r.get("title", "")) for r in results))
    url_w = max(len("URL"), max(len(r.get("url", "")) for r in results))
    snippet_w = max(len("Snippet"), max(len(r.get("snippet", "")) for r in results))
    header = f"{'Pos':>3}  {title_w*' '}  {url_w*' '}  {snippet_w*' '}"
    print(header)
    print("-" * len(header))
    for i, r in enumerate(results, 1):
        title = r.get("title", "")[:title_w]
        url = r.get("url", "")[:url_w]
        snippet = r.get("snippet", "")[:snippet_w]
        print(f"{i:>3}  {title:<{title_w}}  {url:<{url_w}}  {snippet:<{snippet_w}}")

if __name__ == "__main__":
    # 1. Parse JSON input if passed by aichat's tool dispatcher
    if len(sys.argv) > 1 and (sys.argv[1].startswith("{") or sys.argv[1].startswith("[")):
        try:
            kwargs = json.loads(sys.argv[1])
            query_val = kwargs.get("query")
            limit_val = kwargs.get("limit", 10)
            fmt_val = kwargs.get("export_format", "json")  # Default to json when called programmatically
            
            if not query_val:
                print(json.dumps([{"error": "Query is required"}]))
                sys.exit(1)
                
            run(
                query_val,
                limit=limit_val,
                include_domains=kwargs.get("include_domains"),
                exclude_domains=kwargs.get("exclude_domains"),
                date_filter=kwargs.get("date_filter"),
                site_filter=kwargs.get("site_filter"),
                file_type=kwargs.get("file_type"),
                lang=kwargs.get("lang"),
                safe=kwargs.get("safe", False),
                export_format=fmt_val,
            )
            sys.exit(0)
        except Exception as err:
            print(json.dumps({"error": str(err), "status": "failed"}, indent=2))
            sys.exit(1)

    # 2. Fallback to standard CLI arguments
    parser = argparse.ArgumentParser(description="Web search with real API")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--export-format",
        choices=["json", "csv", "md", "html", "table"],
        default="table",
    )
    args = parser.parse_args()
    try:
        run(
            args.query,
            limit=args.limit,
            export_format=args.export_format,
        )
    except Exception as e:
        print(json.dumps({"error": str(e), "status": "failed"}, indent=2))
        sys.exit(1)
