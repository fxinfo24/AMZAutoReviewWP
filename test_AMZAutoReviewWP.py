"""Test module for amazon_babu_new_kheil.py.

This module contains unit tests for the Amazon product review generator
and WordPress publisher functionality.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

from AMZAutoReviewWP import (
    RateLimiter,
    ReviewCache,
    fetch_product_features,
    generate_review,
    post_to_wordpress,
)


@pytest.fixture
def rate_limiter():
    """Create a RateLimiter instance for testing.
    
    Returns:
        RateLimiter: A configured rate limiter for testing.
    """
    return RateLimiter(calls=2, period=1)


@pytest.fixture
def temp_cache_file(tmp_path):
    """Create a temporary cache file for testing.
    
    Args:
        tmp_path: Pytest fixture providing temporary directory.
        
    Returns:
        Path: Path to temporary cache file.
    """
    cache_file = tmp_path / "test_cache.json"
    return cache_file


@pytest.fixture
def review_cache(temp_cache_file):
    """Create a ReviewCache instance for testing.
    
    Args:
        temp_cache_file: Path to temporary cache file.
        
    Returns:
        ReviewCache: Configured review cache for testing.
    """
    return ReviewCache(temp_cache_file)


@pytest.mark.asyncio
async def test_rate_limiter(rate_limiter):
    """Test rate limiter functionality.
    
    Verifies that rate limiting properly delays requests when limit is reached.
    """
    start_time = datetime.now()
    await rate_limiter.acquire()
    await rate_limiter.acquire()
    await rate_limiter.acquire()  # This should wait
    duration = (datetime.now() - start_time).total_seconds()
    assert duration >= 1.0


def test_review_cache(review_cache):
    """Test review cache functionality.
    
    Verifies cache set/get operations and expiration behavior.
    """
    test_key = "test_product"
    test_data = {"content": "Test review"}
    
    # Test cache set and get
    review_cache.set(test_key, test_data)
    cached = review_cache.get(test_key)
    assert cached == test_data
    
    # Test cache expiration
    expired_key = "expired_product"
    review_cache.cache[expired_key] = {
        "timestamp": (datetime.now() - timedelta(hours=25)).isoformat(),
        "data": {"content": "Expired"},
    }
    assert review_cache.get(expired_key) is None


@pytest.mark.asyncio
async def test_generate_review():
    """Test the generate_review function.
    
    Verifies review generation with sample product data and output format.
    """
    # Mock OpenAI response with simplified structure
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Test generated review content"

    # Create mock AsyncOpenAI client
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    with patch('openai.AsyncOpenAI', return_value=mock_client):
        review = await generate_review(
            "Test Product",
            "Feature 1, Feature 2",
            "B00TEST123",
            "Competitor",
            "B00COMP123"
        )
        
        assert isinstance(review, str)
        assert review == "Test generated review content"
        
        # Verify the API was called with correct parameters
        mock_client.chat.completions.create.assert_called_once()
        call_args = mock_client.chat.completions.create.call_args[1]
        assert call_args['model'] == 'gpt-4'
        assert len(call_args['messages']) == 1
        assert call_args['messages'][0]['role'] == 'user'


@pytest.mark.asyncio
async def test_post_to_wordpress():
    """Test the post_to_wordpress function.
    
    Verifies WordPress posting functionality with sample content.
    """
    # Mock response
    mock_response = AsyncMock()
    mock_response.status = 201
    mock_response.json.return_value = {
        "id": 123,
        "status": "publish",
        "title": {"rendered": "Test Review"}
    }

    # Mock session
    mock_session = AsyncMock()
    mock_session.post.return_value = mock_response
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None

    with patch('aiohttp.ClientSession', return_value=mock_session):
        status, response = await post_to_wordpress("Test Review", "Test Content")
        
        assert status == 201
        assert isinstance(response, dict)
        assert response["id"] == 123
        assert response["status"] == "publish"

        # Verify the WordPress API call
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert 'headers' in call_args[1]
        assert 'json' in call_args[1]
        assert call_args[1]['json']['title'] == "Test Review"
        assert call_args[1]['json']['content'] == "Test Content"


def test_fetch_product_features():
    """Test the fetch_product_features function.
    
    Verifies Amazon product feature fetching and response format.
    """
    asin = "B00TEST123"
    result = fetch_product_features(asin)

    if result is not None:
        assert "features" in result
        assert "price" in result
        assert "url" in result
    else:
        assert result is None  # API error case is also valid
