from urllib.parse import urlparse

def is_valid_post_link(href: str) -> bool:
    if not href:
        return False
    parsed = urlparse(href)
    if "facebook.com" not in parsed.netloc.lower():
        return False
        
    path = parsed.path.lower().rstrip("/")
    if path.endswith("/permalink.php") and "story_fbid" in parsed.query:
        return True
        
    parts = path.split("/")
    for marker in ("posts", "photos", "videos", "story.php"):
        if marker in parts:
            idx = parts.index(marker)
            if idx < len(parts) - 1 and parts[idx + 1]:
                return True
    return False

test_cases = [
    ("https://www.facebook.com/fmcs.univbba/posts/pfbid0ybP", True),
    ("https://www.facebook.com/fmcs.univbba/posts", False),
    ("https://www.facebook.com/fmcs.univbba/photos/a.123/456", True),
    ("https://www.facebook.com/fmcs.univbba/photos", False),
    ("https://www.facebook.com/permalink.php?story_fbid=123&id=456", True),
    ("https://www.facebook.com/permalink.php?id=456", False),
    ("https://www.facebook.com/fmcs.univbba", False),
    ("https://google.com/posts/123", False),
]

for url, expected in test_cases:
    res = is_valid_post_link(url)
    assert res == expected, f"Failed for {url}: expected {expected}, got {res}"
    print(f"PASS: {url} -> {res}")
