import os
import time
import json
import random
import requests
import PIL.Image
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv
# from google import genai
# from google.genai import types
from groq import Groq

load_dotenv()

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("[Warning] playwright-stealth not installed. Run: pip install playwright-stealth")

for i in range(5):
    print(i,os.getenv("REDDIT_USERNAME"+str(i)))

user_number = input()


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
USERNAME = os.getenv("REDDIT_USERNAME"+str(user_number))
PASSWORD = os.getenv("REDDIT_PASSWORD"+str(user_number))
COOKIE_FILE = f"reddit_cookies{str(user_number)}.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) "
                  "Gecko/20100101 Firefox/122.0"
}


# ─────────────────────────────────────────────
# READING — Public JSON (no login needed)
# ─────────────────────────────────────────────

def get_subreddit_posts(subreddit: str, sort: str = "hot", limit: int = 10) -> list[dict]:
    """Fetch posts from a subreddit. sort: 'hot', 'new', 'top', 'rising'"""
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}"
    response = requests.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    posts = []
    for child in response.json()["data"]["children"]:
        p = child["data"]
        posts.append({
            "id":           p["id"],
            "title":        p["title"],
            "author":       p["author"],
            "score":        p["score"],
            "url":          p["url"],
            "permalink":    "https://www.reddit.com" + p["permalink"],
            "num_comments": p["num_comments"],
            "selftext":     p.get("selftext", ""),
            "created_utc":  p["created_utc"],
        })
    return posts


def get_post_comments(post_permalink: str, limit: int = 20) -> list[dict]:
    """Fetch top-level comments from a post permalink URL."""
    json_url = post_permalink.rstrip("/") + ".json?limit=" + str(limit)
    response = requests.get(json_url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    comments = []
    for child in response.json()[1]["data"]["children"]:
        if child["kind"] != "t1":
            continue
        c = child["data"]
        comments.append({
            "id":        c["id"],
            "author":    c.get("author", "[deleted]"),
            "body":      c.get("body", ""),
            "score":     c.get("score", 0),
            "permalink": "https://www.reddit.com" + c.get("permalink", ""),
        })
    return comments


def search_reddit(query: str, subreddit: str = None, limit: int = 10) -> list[dict]:
    """Search posts by keyword, optionally scoped to a subreddit."""
    if subreddit:
        url = f"https://www.reddit.com/r/{subreddit}/search.json?q={query}&restrict_sr=1&limit={limit}"
    else:
        url = f"https://www.reddit.com/search.json?q={query}&limit={limit}"
    response = requests.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()
    results = []
    for child in response.json()["data"]["children"]:
        p = child["data"]
        results.append({
            "id":        p["id"],
            "title":     p["title"],
            "subreddit": p["subreddit"],
            "permalink": "https://www.reddit.com" + p["permalink"],
            "score":     p["score"],
        })
    return results



# ─────────────────────────────────────────────
# POSTING — Playwright + Firefox
# ─────────────────────────────────────────────

class Redditbot:
    """
    Firefox-based bot for posting and commenting on Reddit.

    Uses Firefox (not Chromium) because it has a natural TLS fingerprint
    that security scanners don't associate with automation tools.

    Navigation uses window.location.href instead of Playwright's goto()
    so requests have the same Sec-Fetch headers as a real user clicking a link.

    Login strategy:
      1. Load saved cookies → restore session, skip login form entirely.
      2. Otherwise log in via Reddit's normal login page.
         Run with headless=False the first time to solve any CAPTCHA manually.
         Cookies are saved after success so you only ever do this once.
    """

    def __init__(self, username: str, password: str, headless: bool = False,
                 cookie_file: str = COOKIE_FILE):
        self.username    = username
        self.password    = password
        self.headless    = headless
        self.cookie_file = cookie_file
        self._playwright = None
        self._browser    = None
        self._context    = None
        self._page       = None

    def start(self):
        """Launch Firefox and establish a logged-in session."""
        self._playwright = sync_playwright().start()

        # Firefox: real-browser TLS fingerprint, no automation flags to strip
        self._browser = self._playwright.firefox.launch(headless=self.headless)
        self._context = self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            screen={"width": 1920, "height": 1080},
        )

        # Patch navigator.webdriver just in case
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        self._page = self._context.new_page()

        if HAS_STEALTH:
            Stealth(navigator_user_agent=False).apply_stealth_sync(self._page)

        # Try restoring saved cookies first
        if self._load_cookies():
            print(f"[bot{user_number}] Found saved cookies — attempting session restore...")
            self._page.goto("https://www.reddit.com", wait_until="commit")
            self._human_delay(1, 2)
            # self._page.reload(wait_until="domcontentloaded")
            self._human_delay(2, 3)
            if self._is_logged_in():
                print(f"[bot{user_number}] Session restored. No login needed.")
                return
            print(f"[bot{user_number}] Cookies expired. Logging in fresh...")

        self._login()

    def stop(self):
        if self._browser:    self._browser.close()
        if self._playwright: self._playwright.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    # ── Internal helpers ────────────────────────

    def _human_delay(self, min_s: float = 1.0, max_s: float = 3.0):
        time.sleep(random.uniform(min_s, max_s))

    def _goto(self, url: str):
        """
        Navigate to url, wait for it to fully settle, then reload.
        The reload mimics pressing F5 — sends Cache-Control: max-age=0
        which passes WAF checks that block the initial automated request.
        """
        self._page.goto(url, wait_until="commit")
        self._human_delay(1, 2)
        # self._page.reload(wait_until="domcontentloaded")
        self._human_delay(1.5, 3)

    def _is_logged_in(self) -> bool:
        try:
            # We check for several "Authorized Only" elements
            selectors = [
                # 1. The specific 2026 user drawer host
                'shreddit-nav-user-drawer', 
                # 2. The "Create Post" button (only visible if logged in)
                'a[href="/submit"]',
                '#create-post-button',
                # 3. The Notifications/Inbox icon
                'faceplate-tracker[noun="notification_inbox"]',
                # 4. Your specific username in the URL or drawer
                f'a[href*="/user/{self.username}"]'
            ]
            
            for sel in selectors:
                if self._page.locator(sel).first.count() > 0:
                    # Double check it's actually visible, not just in the DOM
                    if self._page.locator(sel).first.is_visible():
                        return True
            
            return False
        except Exception:
            return False

    def _save_cookies(self):
        with open(self.cookie_file, "w") as f:
            json.dump(self._context.cookies(), f, indent=2)
        print(f"[bot{user_number}] Cookies saved to '{self.cookie_file}'. Future runs will skip login.")

    def _load_cookies(self) -> bool:
        if not os.path.exists(self.cookie_file):
            return False
        with open(self.cookie_file) as f:
            self._context.add_cookies(json.load(f))
        return True

    def _login(self):
        """
        Login via Reddit's standard login page.
        Run with headless=False (the default) so you can solve any CAPTCHA.
        Cookies are saved automatically — you only ever need to do this once.
        """
        print(f"[bot{user_number}] Logging in as '{self.username}'...")
        print(f"[bot{user_number}] If a CAPTCHA appears, solve it in the browser window.")
        page = self._page

        page.goto("https://www.reddit.com/login/", wait_until="commit")
        self._human_delay(1, 2)
        page.reload(wait_until="domcontentloaded")
        self._human_delay(2, 3)

        # Click, clear, then type — more reliable than fill() on React inputs
        username_input = page.locator('input[name="username"]')
        username_input.click()
        self._human_delay(0.3, 0.7)
        username_input.click(click_count=3)  # select all existing text
        username_input.type(self.username, delay=random.randint(50, 120))
        self._human_delay(0.5, 1.5)

        password_input = page.locator('input[name="password"]')
        password_input.click()
        self._human_delay(0.3, 0.7)
        password_input.click(click_count=3)
        password_input.type(self.password, delay=random.randint(50, 120))
        self._human_delay(0.8, 1.5)

        # Log what's actually in the fields so we can confirm they were filled
        filled_user = username_input.input_value()
        print(f"[bot{user_number}] Username field contains: '{filled_user}'")

        # Try multiple selectors since Reddit's login button varies
        for selector in [
            'button[type="submit"]',
            'button:has-text("Log In")',
            'button:has-text("Login")',
            'button:has-text("Sign In")',
            'fieldset button',
        ]:
            btn = page.locator(selector)
            if btn.count() > 0:
                btn.first.evaluate("el => el.click()")
                break
        self._human_delay(1, 2)

        # Wait for navigation away from login page (up to 3 min to allow CAPTCHA solving)
        try:
            page.wait_for_url(lambda url: "login" not in url, timeout=180_000)
        except PlaywrightTimeout:
            raise RuntimeError("Login timed out. If there was a CAPTCHA, it wasn't solved in time.")

        self._human_delay(2, 4)

        if "login" in page.url:
            raise RuntimeError("Login failed — check your username and password.")

        print(f"[bot{user_number}] Logged in successfully.")
        self._save_cookies()

    # ── bot actions ─────────────────────────────

    def post_comment(self, post_permalink: str, comment_text: str) -> bool:
        page = self._page
        # print(f"[bot{user_number}] Navigating to: {post_permalink}")
        self._goto(post_permalink)
        self._human_delay(2, 3)

        try:
            # 1. Trigger the composer
            # We use the 'faceplate-textarea-input' identified in your logs
            trigger = page.locator('comment-composer-host [data-testid="trigger-button"]').first
            trigger.scroll_into_view_if_needed()
            trigger.click(force=True)
            self._human_delay(2, 3)

            # 2. Focus the Editor
            # We target the specific div inside the composer revealed by your tracker
            editor_selector = 'shreddit-composer div[role="textbox"]'
            editor = page.locator(editor_selector).first
            
            # Ensure it is visible before we try to type
            editor.wait_for(state="visible", timeout=10000)
            
            # Click it to ensure browser focus is 100% inside the text box
            editor.click()
            self._human_delay(1, 2)

            # 3. Type the comment
            # We use keyboard.type because Lexical (Reddit's editor) needs 
            # hardware-level events to enable the 'Comment' button.
            page.keyboard.type(comment_text, delay=random.randint(10, 20))
            self._human_delay(1, 2)

            # 4. Click the Submit Button
            # Your tracker showed: span[slot="content"] inside the button
            # We will target the button directly using the slot identified earlier
            submit_btn = page.locator('shreddit-composer button[slot="submit-button"]').first
            
            if submit_btn.is_enabled():
                # Real click first
                submit_btn.click()
            else:
                # If Reddit is being stubborn, force the click event
                print(f"[bot{user_number}] Button disabled, forcing click event...")
                submit_btn.dispatch_event("click")

            print(f"[bot{user_number}] commented",comment_text[:20]+"...")
            self._human_delay(5, 7)
            return True

        except Exception as e:
            print(f"[bot{user_number}] Interaction failed: {e}")
            page.screenshot(path="reddit_fail.png")
            return False










    def submit_text_post(self, subreddit_name: str, title: str, body: str) -> bool:
        page = self._page
        submit_url = f"https://www.reddit.com/r/{subreddit_name}/submit"
        print(f"[bot{user_number}] Navigating to: {submit_url}")
        self._goto(submit_url)
        self._human_delay(2, 3)

        try:
            # 1. Title Field
            # Tracker saw: textarea#innerTextArea inside FACEPLATE-TEXTAREA-INPUT
            print(f"[bot{user_number}] Entering title...")
            title_container = page.locator('faceplate-textarea-input[name="title"]').first
            title_container.click() # Focus the container
            page.keyboard.type(title, delay=random.randint(40, 80))
            self._human_delay(1, 2)

            # 2. Handle the Post Body (Rich Text Editor)
            # Just like the comment box, this is often inside a Shadow DOM
            print(f"[bot{user_number}] Focusing post body...")
            # We look for the editor container or the role="textbox" inside shreddit-composer
            editor_selector = 'shreddit-composer div[role="textbox"]'
            editor = page.locator(editor_selector).first
            
            editor.wait_for(state="visible", timeout=15000)
            editor.click()
            self._human_delay(1, 2)

            # Use keyboard.type to ensure Lexical catches the input and enables the "Post" button
            print(f"[bot{user_number}] Typing body content...")
            page.keyboard.type(body, delay=random.randint(30, 70))
            self._human_delay(2, 3)

            # 3. The Submit Button
            # Tracker saw: R-POST-FORM-SUBMIT-BUTTON
            print(f"[bot{user_number}] Attempting to click Post...")
            
            # Target the custom component directly
            submit_host = page.locator('r-post-form-submit-button').first
            
            # We will use dispatch_event on the host OR find the button inside
            # Since the tracker showed a SPAN inside, we'll click the host component 
            # which Reddit listens to for the 'Submit' action.
            if submit_host.count() > 0:
                # Scroll to it to ensure it's in the viewport
                submit_host.scroll_into_view_if_needed()
                self._human_delay(1, 2)
                
                # We use a JavaScript click on the host component to bypass the Shadow DOM barrier
                page.evaluate("""() => {
                    const btnHost = document.querySelector('r-post-form-submit-button');
                    if (btnHost) {
                        // Try clicking the host, or a button inside its shadowRoot
                        const internalBtn = btnHost.shadowRoot ? btnHost.shadowRoot.querySelector('button') : null;
                        if (internalBtn) {
                            internalBtn.click();
                        } else {
                            btnHost.click();
                        }
                    }
                }""")
                
                print(f"[bot{user_number}] Post submission triggered.")
                self._human_delay(5, 8)
                return True
            else:
                print(f"[bot{user_number}] Could not find r-post-form-submit-button")
                return False

        except Exception as e:
            print(f"[bot{user_number}] Failed to create post: {e}")
            page.screenshot(path="post_error.png")
            return False



    def get_random_home_post(self):
        page = self._page
        
        while True:
            print(f"[bot{user_number}] Navigating to home page...")
            
            # 1. Navigation Loop
            while True:
                try:
                    target_url = "https://www.reddit.com/rising/?feed=home&feedViewType=compactView"
                    response = self._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    if response and response.ok:
                        break
                    print(f"[bot{user_number}] Status {response.status if response else 'None'}. Retrying...")
                except Exception as e:
                    print(f"[bot{user_number}] Navigation failed ({type(e).__name__}). Retrying in 5s...")
                    self._human_delay(4, 6)
            
            self._human_delay(2, 3)

            try:
                # 2. Stability Check: Wait for at least one post to render before scrolling
                # This prevents "Execution context was destroyed" errors
                page.wait_for_selector('shreddit-post', state='attached', timeout=15000)

                # 3. Load more posts by scrolling
                for _ in range(3):
                    page.mouse.wheel(0, 4000)
                    self._human_delay(0.4, 0.6)

                # 4. Filter seen URLs with UTF-8 encoding
                seen_urls = set()
                try:
                    with open("log.txt", "r", encoding="utf-8") as f:
                        seen_urls = {line.strip() for line in f if line.strip()}
                except FileNotFoundError:
                    pass

                post_locator = page.locator('shreddit-post:not([ad-id])')
                post_count = post_locator.count()

                if post_count == 0:
                    print(f"[bot{user_number}] No posts found. Refreshing...")
                    continue 

                indices = list(range(post_count))
                random.shuffle(indices)
                
                target_post = None
                permalink = None
                post_title = None
                subreddit_name = None

                for idx in indices:
                    temp_post = post_locator.nth(idx)
                    
                    p_type = temp_post.get_attribute('post-type')
                    domain = temp_post.get_attribute('domain') or ""
                    has_video = temp_post.locator('shreddit-player-2, video, .video-player-wrapper').count() > 0
                    
                    if p_type == "video" or "v.redd.it" in domain or has_video:
                        continue

                    temp_permalink = temp_post.get_attribute('permalink')
                    if temp_permalink and not temp_permalink.startswith('http'):
                        temp_permalink = f"https://www.reddit.com{temp_permalink}"
                    
                    if temp_permalink not in seen_urls:
                        target_post = temp_post
                        permalink = temp_permalink
                        post_title = target_post.get_attribute('post-title')
                        subreddit_name = target_post.get_attribute('subreddit-prefixed-name')
                        break
                
                if not target_post:
                    print(f"[bot{user_number}] No new posts in this batch. Refreshing...")
                    continue 

                # 5. Success! Navigate to the specific post
                print(f"[bot{user_number}] Selected: {post_title} ({subreddit_name})")
                self._goto(permalink)
                self._human_delay(2, 4) # Time for body content to render

                post_data = {
                    "title": post_title,
                    "url": permalink,
                    "subreddit": subreddit_name,
                    "text": "",
                    "image_url": None,
                    "image_path": None,
                    "is_gallery": "/gallery/" in permalink
                }

                # --- IMPROVED EXTRACTION LOGIC ---
                # Focus on the main post's text-body slot to avoid community status tooltips
                main_post = page.locator('shreddit-post').first
                
                # Primary target: The div with the schema property
                body_locator = main_post.locator('[slot="text-body"] div[property="schema:articleBody"]').first
                
                # Fallback: Specific post ID prefix 't3_'
                if body_locator.count() == 0:
                    body_locator = main_post.locator('div[id^="t3_"][id$="-post-rtjson-content"]').first

                if body_locator.count() > 0:
                    paragraphs = body_locator.locator('p').all()
                    text_parts = [p.inner_text().strip() for p in paragraphs if p.inner_text().strip()]
                    
                    # Remove any noise like 'sh.reddit.com' links
                    post_data["text"] = "\n\n".join([t for t in text_parts if "sh.reddit.com" not in t])
                    
                    preview = (post_data["text"][:75] + '...') if len(post_data["text"]) > 75 else post_data["text"]
                    print(f"[bot{user_number}] selected text: {preview}")
                else:
                    print(f"[bot{user_number}] Could not find post body text.")
                # --------------------------------------------

                # Handle Image URLs
                img_locator = main_post.locator('img[src^="https://preview.redd.it"], figure img').first
                if img_locator.count() > 0:
                    post_data["image_url"] = img_locator.get_attribute('src')

                return post_data

            except Exception as e:
                # Catches "Execution context destroyed" and other DOM-related crashes
                print(f"[bot{user_number}] Error during process: {e}. Retrying...")
                self._human_delay(2, 4)





# def generate_reddit_comment(api_key, prompt_file, subreddit, title, text, image_path, retries=5):
#     client = genai.Client(api_key=api_key)
    
#     with open(prompt_file, 'r') as f:
#         template = f.read()

#     config = types.GenerateContentConfig(
#         system_instruction="""You are a Redditor. Write exactly ONE paragraph. 
#         Never use double line breaks. Be concise but descriptive.""",
#         temperature=0.8,
#         max_output_tokens=1024,
#         # This is the 'kill switch' for extra paragraphs
#         stop_sequences=["\n\n"], 
#         # thinking_config={'include_thoughts': False, 'thinking_level': 'low'} 
#     )

#     formatted_prompt = template.format(subreddit=subreddit, title=title, text=text)
#     if image_path:
#         contents = [formatted_prompt, PIL.Image.open(image_path)]
#     else:
#         contents = [formatted_prompt]

#     # --- RETRY LOGIC ---
#     for attempt in range(retries):
#         try:
#             response = client.models.generate_content(
#                 model="gemini-2.5-flash-lite", 
#                 contents=contents,
#                 config=config
#             )
#             if "RESOURCE_EXHAUSTED" not in response.text:
#                 return response.text.lower()
            
#         except genai.errors.ServerError as e:
#             if "503" in str(e) and attempt < retries - 1:
#                 wait_time = (2 ** attempt) # 1s, 2s, 4s, 8s...
#                 print(f"Server overloaded. Retrying in {wait_time}s...")
#                 time.sleep(wait_time)
#                 continue
#             else:
#                 print(f"Final failure after {retries} attempts: {e}")
#                 return ""
#         except Exception as e:
#             print(f"Non-server error: {e}")
#             return ""



def generate_reddit_comment(api_key, prompt_file, system_file, subreddit, title, text, image_url, retries=5):
    # Initialize the Groq client
    client = Groq(api_key=api_key)
    
    with open(prompt_file, 'r') as f:
        template = f.read()

    with open(system_file, 'r') as f:
        system = f.read()

    # Prepare the formatted prompt
    formatted_prompt = template.format(subreddit=subreddit, title=title, text=text)
    log_information = """Subreddit: {subreddit}
Title: {title}
Text: {text}"""
    print(log_information.format(subreddit=subreddit, title=title, text=text))

    # Groq uses a 'messages' list rather than 'contents'
    if image_url:
        messages = [
            {
                "role": "system",
                "content": system
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": formatted_prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }
        ]
    else:
        messages = [
            {
                "role": "system",
                "content": system
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": formatted_prompt}
                ]
            }
        ]

    # --- RETRY LOGIC ---
    for attempt in range(retries):
        try:
            completion = client.chat.completions.create(
                # Llama 3.2 11B is the current high-volume vision king on Groq
                model="meta-llama/llama-4-scout-17b-16e-instruct", 
                messages=messages,
                temperature=1,
                max_tokens=1024, # Keep it small to prevent rambling
                top_p=0.9,
                stream=False,
                stop=["\n"] # Hard cut-offs for rambling
            )
            
            comment = completion.choices[0].message.content
            return comment.strip()
            
        except Exception as e:
            # Groq returns specific 429 for Rate Limits and 503 for Overload
            if ("429" in str(e) or "503" in str(e)) and attempt < retries - 1:
                wait_time = (2 ** attempt)
                print(f"Groq busy or limit hit. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"Groq error: {e}")
                return ""

# --- Usage ---
# print(generate_reddit_comment("API_KEY", "prompt.txt", "Title here", "Body here", "pic.jpg"))

# ─────────────────────────────────────────────
# EXAMPLE USAGE
# ─────────────────────────────────────────────

if __name__ == "__main__":

    # Reading — no login needed
    # posts = get_subreddit_posts("Python", sort="hot", limit=5)
    # for post in posts:
    #     print(f"[{post['score']:>5}] {post['title']}")

    with Redditbot(USERNAME, PASSWORD, headless=True) as bot:
        while True:
            post_data = bot.get_random_home_post()
            comment = generate_reddit_comment(
                api_key=os.getenv("GROQ_API_KEY"+str(user_number)),
                prompt_file="prompt.txt",
                system_file="system.txt",
                subreddit=post_data['subreddit'],
                title=post_data['title'],
                text=post_data['text'],
                image_url=post_data['image_url']
            )
            print("[comment]",comment)
            if comment != "":
                bot.post_comment(post_data['url'], comment)
                with open("log.txt", "r+", encoding="utf-8") as f:
                    old_content = f.read()
                    f.seek(0)
                    f.write(f"\n{post_data['url']}\n{comment}\n"+old_content)