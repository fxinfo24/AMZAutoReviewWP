"""Amazon product review generator and WordPress publisher module.

This module provides functionality to generate product reviews using OpenAI's GPT model
and publish them to WordPress. Features include rate limiting, caching, retry logic,
and async processing for optimal performance.
"""

import asyncio
import base64
import csv
import html
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from bs4 import BeautifulSoup

from dotenv import load_dotenv

import openai

import requests

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from tqdm import tqdm

# Load environment variables
load_dotenv()

# Configuration
WORDPRESS_USER = os.getenv('WORDPRESS_USER', 'your_wordpress_username')
WORDPRESS_PASSWORD = os.getenv(
    'WORDPRESS_PASSWORD',
    'your_wordpress_application_password'
)
WORDPRESS_URL = os.getenv(
    'WORDPRESS_URL',
    'https://your-wordpress-site.com/wp-json/wp/v2/posts'
)
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'sk-your-openai-api-key')
# Options: gpt-4, gpt-4-turbo-preview, gpt-3.5-turbo
GPT_MODEL = os.getenv('GPT_MODEL', 'gpt-4')
TEMPERATURE = float(os.getenv('TEMPERATURE', 0.7))  # Controls creativity (0.0-1.0)
MAX_TOKENS = int(os.getenv('MAX_TOKENS', 1500))  # Maximum length of generated reviews
AMAZON_ACCESS_KEY = os.getenv('AMAZON_ACCESS_KEY', 'your_amazon_access_key')
AMAZON_SECRET_KEY = os.getenv('AMAZON_SECRET_KEY', 'your_amazon_secret_key')
AMAZON_ASSOCIATE_TAG = os.getenv('AMAZON_ASSOCIATE_TAG', 'your_amazon_associate_tag')
AMAZON_MARKETPLACE = os.getenv('AMAZON_MARKETPLACE', 'US')  # Options: US, UK, CA, etc.
MAX_CONCURRENT_REQUESTS = int(os.getenv('MAX_CONCURRENT_REQUESTS', 5))
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', 30))
CACHE_TTL_HOURS = int(os.getenv('CACHE_TTL_HOURS', 24))

# Constants
CACHE_DIR = Path('cache')
CACHE_FILE = CACHE_DIR / 'review_cache.json'
LOG_DIR = Path('logs')
LOG_FILE = LOG_DIR / f'review_generator_{datetime.now():%Y%m%d}.log'

# Setup directories
CACHE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class RateLimiter:
    """Rate limiter for API calls."""

    def __init__(self, calls: int, period: int):
        """Initialize rate limiter.

        Args:
            calls: Maximum number of calls allowed
            period: Time period in seconds
        """
        self.calls = calls
        self.period = period
        self.timestamps: List[float] = []

    async def acquire(self):
        """Acquire a rate limit token."""
        now = datetime.now().timestamp()
        self.timestamps = [ts for ts in self.timestamps if ts > now - self.period]

        if len(self.timestamps) >= self.calls:
            sleep_time = self.timestamps[0] - (now - self.period)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        self.timestamps.append(now)


class ReviewCache:
    """Cache for generated reviews."""

    def __init__(self, cache_file: Path):
        """Initialize review cache.

        Args:
            cache_file: Path to cache file
        """
        self.cache_file = cache_file
        self.cache: Dict[str, Any] = self._load_cache()

    def _load_cache(self) -> Dict:
        if self.cache_file.exists():
            return json.loads(self.cache_file.read_text())
        return {}

    def save_cache(self):
        """Save cache to file."""
        self.cache_file.write_text(json.dumps(self.cache, indent=2))

    def get(self, key: str) -> Optional[Dict]:
        """Get cached review if not expired."""
        if key in self.cache:
            cached = self.cache[key]
            cache_time = datetime.fromisoformat(cached['timestamp'])
            if cache_time > datetime.now() - timedelta(hours=CACHE_TTL_HOURS):
                return cached['data']
        return None

    def set(self, key: str, data: Dict):
        """Cache review data."""
        self.cache[key] = {
            'timestamp': datetime.now().isoformat(),
            'data': data
        }
        self.save_cache()


def save_reviews_to_csv(reviews: List[Dict]) -> None:
    """Save generated reviews to a CSV file.

    Args:
        reviews: List of review dictionaries containing name, title, rating, and text
    """
    fieldnames = ["name", "title", "rating", "text", "price", "url"]
    with open('reviews.csv', mode='w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for review in reviews:
            writer.writerow(review)


def fetch_product_features(asin: str) -> Optional[Dict[str, Any]]:
    """Fetch product features from Amazon using the Product Advertising API."""
    url = f"https://api.amazon.com/products/{asin}"
    params = {
        'AssociateTag': AMAZON_ASSOCIATE_TAG,
        'AccessKey': AMAZON_ACCESS_KEY,
        'SecretKey': AMAZON_SECRET_KEY,
        'ASIN': asin
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        product_data = response.json()

        features = product_data.get('ItemAttributes', {}).get('Feature', [])
        price = product_data.get('OfferSummary', {}).get(
            'LowestNewPrice', {}
        ).get('FormattedPrice', 'N/A')

        return {
            'features': features,
            'price': price,
            'url': product_data.get('DetailPageURL', '')
        }
    except Exception as e:
        logger.error(f"Error fetching product features for ASIN {asin}: {str(e)}")
        return None


# Initialize rate limiters
# API rate limit for OpenAI (default 20 calls per minute = 60 seconds period)
openai_limiter = RateLimiter(
    calls=int(os.getenv('OPENAI_CALLS_PER_MINUTE', 20)),
    period=int(os.getenv('OPENAI_PERIOD_SECONDS', 60))
)
# API rate limit for WordPress (default 30 calls per minute = 60 seconds period)
wp_limiter = RateLimiter(
    calls=int(os.getenv('WP_CALLS_PER_MINUTE', 30)),
    period=int(os.getenv('WP_PERIOD_SECONDS', 60))
)
cache = ReviewCache(CACHE_FILE)


@retry(
    retry=retry_if_exception_type((aiohttp.ClientError, openai.OpenAIError)),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(3)
)
async def generate_review(
    product_name: str,
    product_features: str,
    asin: str,
    competitor_name: str,
    competitor_asin: str,
    temperature: float = TEMPERATURE,
    max_tokens: int = MAX_TOKENS
) -> str:
    """Generate a structured product review using OpenAI's GPT model."""
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    prompt = (
        f"Generate a detailed product review for '{product_name}' (ASIN: {asin}) "
        f"with the following features: {product_features}. "
        f"Include the following sections: AI Title, Feature Image, Meta Description, "
        f"Intro, Description, Image-1, Features, Pros, Check Price Button, "
        f"Image-2, Cons, Comparison to {competitor_name} (ASIN: {competitor_asin}), "
        f"Check Price Button, YouTube Video, Conclusion, and FAQ (10). "
        f"Use English language."
    )

    response = await client.chat.completions.create(
        model=GPT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens
    )

    return response.choices[0].message.content


@retry(
    retry=retry_if_exception_type(aiohttp.ClientError),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(3)
)
async def post_to_wordpress(title: str, content: str) -> Tuple[int, Dict]:
    """Post a review to WordPress with retry logic."""
    wordpress_credentials = f"{WORDPRESS_USER}:{WORDPRESS_PASSWORD}"
    wordpress_token = base64.b64encode(wordpress_credentials.encode()).decode()

    headers = {
        'Authorization': f'Basic {wordpress_token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    data = {
        'title': title,
        'content': content,
        'status': 'publish'
    }

    async with aiohttp.ClientSession() as session:
        response = await session.post(
            WORDPRESS_URL,
            headers=headers,
            json=data,
            raise_for_status=True
        )
        return response.status, await response.json()


async def process_product(product: Dict) -> Optional[Dict]:
    """Process a single product review generation and posting."""
    try:
        asin = product['asin']
        competitor_name = "Competitor Product Name"
        competitor_asin = "Competitor ASIN"
        amazon_data = fetch_product_features(asin)

        if not amazon_data:
            logger.error(f"Could not fetch features for ASIN {asin}")
            return None

        product_features = ', '.join(amazon_data['features'])
        product_url = amazon_data['url']

        cache_key = f"{product['product_name']}_{hash(product_features)}"

        if cached := cache.get(cache_key):
            logger.info(f"Using cached review for {product['product_name']}")
            return cached

        await openai_limiter.acquire()

        content = await generate_review(
            product['product_name'],
            product_features,
            asin,
            competitor_name,
            competitor_asin
        )

        # Sanitize content for WordPress
        soup = BeautifulSoup(content, 'html.parser')
        content = str(soup)
        content = html.escape(content)

        button_style = (
            "display:inline-block; padding:10px; "
            "background-color:#0073aa; color:white; "
            "text-decoration:none; border-radius:5px;"
        )
        content_with_price_link = (
            f"{content}<br>"
            f"<a href='{product_url}' target='_blank' style='{button_style}'>"
            f"Check the latest price</a>"
        )

        result = await post_to_wordpress(
            product['product_name'],
            content_with_price_link
        )
        status_code, response = result

        if status_code == 201:
            cache.set(cache_key, {"content": content_with_price_link})
            logger.info(f"Successfully processed review for {product['product_name']}")
            return {"content": content_with_price_link}

        logger.error(
            f"Failed to post review for {product['product_name']}: Status {status_code}"
        )
        return None

    except Exception as e:
        logger.error(
            f"Error processing {product['product_name']}: {str(e)}",
            exc_info=True
        )
        return None


async def main():
    """Execute the main program flow."""
    reviews = []

    try:
        with open('products.csv', mode='r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            products = list(reader)

        logger.info(f"Processing {len(products)} products")
        
        with tqdm(total=len(products)) as pbar:
            tasks = []
            for product in products:
                if len(tasks) >= MAX_CONCURRENT_REQUESTS:
                    done, tasks = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=REQUEST_TIMEOUT
                    )
                    reviews.extend([t.result() for t in done if t.result()])
                    pbar.update(len(done))

                tasks.append(asyncio.create_task(process_product(product)))

            if tasks:
                done, _ = await asyncio.wait(tasks, timeout=REQUEST_TIMEOUT)
                reviews.extend([t.result() for t in done if t.result()])
                pbar.update(len(done))

        if reviews:
            save_reviews_to_csv(reviews)
            logger.info(f"Saved {len(reviews)} reviews to reviews.csv")
        else:
            logger.warning("No reviews were generated")

    except FileNotFoundError:
        logger.error("products.csv file not found")
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
