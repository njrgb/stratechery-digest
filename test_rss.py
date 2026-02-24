import feedparser
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def inspect_feed(url, label):
    print(f"\n{'='*60}")
    print(f"{label}")
    feed = feedparser.parse(url)
    status = getattr(feed, 'status', 'unknown')
    print(f"HTTP status : {status}  |  Entries: {len(feed.entries)}")

    if not feed.entries:
        print("No entries returned.")
        return

    # Show content length for all entries
    print(f"\n{'#':<3} {'Chars':<8} {'Title'}")
    print("-" * 60)
    for i, entry in enumerate(feed.entries):
        content = entry.get('content', [{}])[0].get('value', '') or entry.get('summary', '')
        print(f"{i:<3} {len(content):<8} {entry.get('title', 'N/A')[:55]}")

    # Deep inspect the latest entry
    latest = feed.entries[0]
    content = latest.get('content', [{}])[0].get('value', '') or latest.get('summary', '')
    paywall_hints = ['subscribe', 'paid subscriber', 'upgrade', 'this post is for']
    found = [h for h in paywall_hints if h in content.lower()]
    print(f"\n--- Latest entry detail ---")
    print(f"Title    : {latest.get('title', 'N/A')}")
    print(f"Paywall? : {'YES — ' + str(found) if found else 'No obvious signal'}")
    print(f"Tail (last 300 chars):\n{content[-300:].strip()}")

inspect_feed(
    "https://every.to/feeds/f9df86be4041a029e161.xml",
    "Every (subscriber feed)"
)
