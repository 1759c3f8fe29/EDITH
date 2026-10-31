import urllib.request
import re

def main():
    url = 'https://example.com'
    
    # Fetch HTML
    with urllib.request.urlopen(url) as response:
        html = response.read().decode('utf-8')
    
    # Extract text content (remove scripts, styles, and tags)
    # Remove script and style elements
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', html)
    # Decode HTML entities
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    # Count words
    words = text.split()
    word_count = len(words)
    
    # Write summary report
    report = f"""Summary Report for {url}
{'=' * 50}
Total words: {word_count}
Total characters (with spaces): {len(text)}
Total characters (without spaces): {len(text.replace(' ', ''))}

Extracted text preview (first 500 chars):
{text[:500]}...
"""
    
    with open('summary_report.txt', 'w') as f:
        f.write(report)
    
    print(f"Done! Word count: {word_count}")
    print("Report written to summary_report.txt")

if __name__ == '__main__':
    main()
