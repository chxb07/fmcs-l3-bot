from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

def normalize_post_url(url: str) -> str:
    if not url:
        return ""
    
    # Strip fragment (#...)
    url_without_frag = url.split("#")[0]
    parsed = urlparse(url_without_frag)
    
    # Standardize hostname
    netloc = parsed.netloc.lower()
    if "facebook.com" in netloc:
        netloc = "www.facebook.com"
        
    path = parsed.path
    # Remove trailing slash for path consistency (except root path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
        
    # Allowed query params for special URLs like permalink.php or photo.php
    ALLOWED_PARAMS = {"story_fbid", "id", "fbid", "v", "set"}
    query_tuples = parse_qsl(parsed.query)
    clean_query_tuples = [(k, v) for k, v in query_tuples if k in ALLOWED_PARAMS]
    clean_query = urlencode(clean_query_tuples)
    
    return urlunparse((parsed.scheme or "https", netloc, path, "", clean_query, ""))

test_urls = [
    "https://web.facebook.com/fmcs.univbba/posts/pfbid0ybPvogWdV91vfmCMGc12xeZuyRoxVCk1GrLpEydcTPuq7Vejpy1CY3VAjDyHewwCl?__cft__[0]=AZiugHg0zsA89UUYkpB25Rhx5f0gGg0URBYE0EZhuUaAQYxWHChXo5Kt5Eg_BUl8QYarv99np85OYs060YF9X53ujfgXCjyu3VLtSARNGVbdStUjsZMe23Rw_7tBOdc964V49BDGv8KrT48RNfJan4VQ6cUvgTIQCOuK_BWJnS5BnQjbjCYpPvmQS9omgY0&__tn__=%2CO%2CP-R",
    "https://www.facebook.com/fmcs.univbba/posts/pfbid0ybPvogWdV91vfmCMGc12xeZuyRoxVCk1GrLpEydcTPuq7Vejpy1CY3VAjDyHewwCl/",
    "https://m.facebook.com/permalink.php?story_fbid=123456&id=789012&mibextid=7654",
    "https://www.facebook.com/fmcs.univbba/photos/a.123/456/?type=3&source=48#comments"
]

for raw in test_urls:
    print(f"RAW: {raw}")
    print(f"NORM: {normalize_post_url(raw)}\n")
