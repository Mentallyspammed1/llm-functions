#!/usr/bin/env python3
# @describe A web fetching utility that uses Python's built-in urllib.request.
# It supports various HTTP methods, headers, timeouts, and SSL verification options.
# @option --action! <fetch> The only supported action is 'fetch'.
# @option --url <TEXT> The URL to fetch. Required if action is 'fetch'.
# @option --method=GET <GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS> HTTP method to use.
# @option --data <TEXT> Data to send in the request body (e.g., for POST/PUT).
# @option --headers <TEXT> Custom headers, comma-separated (e.g., "Header1: Value1,Header2: Value2").
# @option --timeout=30 <INT> Total request timeout in seconds.
# @option --connect-timeout=10 <INT> Connection timeout in seconds (conceptual; urllib uses a single timeout).
# @flag --follow-redirects Follow HTTP redirects (default: True). (Note: urllib follows redirects by default).
# @option --max-redirects=5 <INT> Maximum number of redirects to follow. (Note: This is conceptual for urllib and not directly controllable without custom handlers).
# @flag --verify-ssl Verify SSL certificates (default: True). If False, uses an unverified context.
# @option --user-agent=Python_urllib/1.0 <STRING> Custom User-Agent header.

import os
import urllib.request
import urllib.error
import urllib.parse
import ssl
from typing import Optional, List, Tuple

def fetch_url(
    url: str,
    method: str = "GET",
    data: Optional[str] = None,
    headers_str: Optional[str] = None,
    timeout: int = 30,
    connect_timeout: int = 10, # Conceptual: urllib uses a single timeout value
    follow_redirects: bool = True, # Conceptual: urllib follows redirects by default
    max_redirects: int = 5, # Conceptual: not directly controllable via standard urllib
    verify_ssl: bool = True,
    user_agent: str = "Python_urllib/1.0"
) -> str:
    """
    Fetches content from a URL using Python's urllib.request.

    Args:
        url (str): The URL to fetch.
        method (str): The HTTP method (GET, POST, PUT, DELETE, etc.). Defaults to "GET".
        data (Optional[str]): Data to send in the request body. Defaults to None.
        headers_str (Optional[str]): A comma-separated string of headers. Defaults to None.
        timeout (int): Total timeout in seconds for the request. Defaults to 30.
        connect_timeout (int): Connection timeout in seconds. Conceptual mapping. Defaults to 10.
        follow_redirects (bool): Whether to follow redirects. Defaults to True.
        max_redirects (int): Maximum number of redirects to follow. Conceptual mapping. Defaults to 5.
        verify_ssl (bool): Whether to verify SSL certificates. Defaults to True.
        user_agent (str): Custom User-Agent string. Defaults to "Python_urllib/1.0".

    Returns:
        str: The fetched content or an error message.
    """

    request_headers = {}
    if headers_str:
        try:
            for header_pair in headers_str.split(','):
                if ':' in header_pair:
                    key, value = header_pair.split(':', 1)
                    request_headers[key.strip()] = value.strip()
        except Exception as e:
            return f"Error parsing headers: {e}. Please ensure headers are comma-separated key:value pairs."

    request_headers['User-Agent'] = user_agent

    request_body = None
    if data and method.upper() in ["POST", "PUT", "PATCH", "DELETE"]:
        if 'Content-Type' not in request_headers:
            # Default to form-urlencoded if not specified and data is provided
            request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
            try:
                # Attempt to parse simple key=value&key2=value2 format
                parsed_data = dict(item.split('=', 1) for item in data.split('&'))
                request_body = urllib.parse.urlencode(parsed_data).encode('utf-8')
            except ValueError:
                # Fallback to raw bytes if data format is unexpected
                request_body = data.encode('utf-8')
        else:
            # If Content-Type is explicitly set (e.g., application/json), encode data directly
            request_body = data.encode('utf-8')

    try:
        req = urllib.request.Request(url, data=request_body, method=method.upper(), headers=request_headers)

        ssl_context = None
        if not verify_ssl:
            # Create an unverified SSL context if verification is disabled
            ssl_context = ssl._create_unverified_context()
        
        # urllib.request.urlopen handles redirects automatically by default.
        # Explicitly limiting redirects or disabling them requires custom handler logic
        # which is beyond the scope of a simple tool. The 'timeout' parameter
        # serves as the primary control for request duration.

        with urllib.request.urlopen(req, context=ssl_context, timeout=timeout) as response:
            content = response.read().decode('utf-8', errors='ignore')
            
            status_line = f"--- HTTP {response.getcode()} {response.reason} ---"
            header_lines = [f"{key}: {value}" for key, value in response.getheaders()]
            
            return f"{status_line}\n" + "\n".join(header_lines) + f"\n\n{content}"
            
    except urllib.error.HTTPError as e:
        # Attempt to read error body for more context
        try:
            error_content = e.read().decode('utf-8', errors='ignore')
            return f"Error: HTTP {e.getcode()} {e.reason}\n{error_content}"
        except Exception: # If reading error body fails
            return f"Error: HTTP {e.getcode()} {e.reason}"
    except urllib.error.URLError as e:
        # Catches network errors, including connection timeouts
        return f"Error fetching URL: {e.reason}"
    except Exception as e:
        # Catch any other unexpected errors
        return f"An unexpected error occurred: {str(e)}"

def run(action: str, url: Optional[str] = None, method: str = "GET",
        data: Optional[str] = None, headers: Optional[str] = None,
        timeout: int = 30, connect_timeout: int = 10,
        follow_redirects: bool = True, max_redirects: int = 5,
        verify_ssl: bool = True, user_agent: str = "Python_urllib/1.0") -> str:
    
    if action == "fetch":
        if not url:
            return "Error: --url is required for the 'fetch' action."
        
        return fetch_url(
            url=url,
            method=method,
            data=data,
            headers_str=headers,
            timeout=timeout,
            connect_timeout=connect_timeout, # Conceptual mapping
            follow_redirects=follow_redirects, # Conceptual mapping
            max_redirects=max_redirects, # Conceptual mapping
            verify_ssl=verify_ssl,
            user_agent=user_agent
        )
    else:
        return "Error: Invalid action. Only 'fetch' is supported."

def main():
    # argc injects parameters as environment variables prefixed with 'argc_'
    action = os.environ.get("argc_action", "")
    url = os.environ.get("argc_url", "")
    method = os.environ.get("argc_method", "GET")
    data = os.environ.get("argc_data", None)
    headers_str = os.environ.get("argc_headers", None)
    timeout = int(os.environ.get("argc_timeout", "30"))
    connect_timeout = int(os.environ.get("argc_connect_timeout", "10"))
    follow_redirects_str = os.environ.get("argc_follow_redirects", "true")
    follow_redirects = follow_redirects_str.lower() == "true"
    max_redirects = int(os.environ.get("argc_max_redirects", "5"))
    verify_ssl_str = os.environ.get("argc_verify_ssl", "true")
    verify_ssl = verify_ssl_str.lower() == "true"
    user_agent = os.environ.get("argc_user_agent", "Python_urllib/1.0")
    
    result = run(
        action=action,
        url=url,
        method=method,
        data=data,
        headers=headers_str,
        timeout=timeout,
        connect_timeout=connect_timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        verify_ssl=verify_ssl,
        user_agent=user_agent
    )
    
    # Write result to LLM_OUTPUT file or stdout if LLM_OUTPUT is not set
    output_path = os.environ.get("LLM_OUTPUT")
    if output_path:
        try:
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(result + "\n")
        except IOError as e:
            print(f"Error writing to LLM_OUTPUT file '{output_path}': {e}", file=sys.stderr)
            print(result) # Print to stdout as fallback
    else:
        print(result)

if __name__ == "__main__":
    import sys # Import sys here for stderr if needed in main
    main()
