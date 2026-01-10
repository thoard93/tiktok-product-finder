import sys
from unittest.mock import MagicMock, patch

# Mock Flask and SQLAlchemy before importing app
sys.modules['flask'] = MagicMock()
sys.modules['flask_sqlalchemy'] = MagicMock()
sys.modules['sqlalchemy'] = MagicMock()
sys.modules['sqlalchemy.orm'] = MagicMock()
sys.modules['sqlalchemy.pool'] = MagicMock()

# Import the function to test
# We need to mock a lot of things because app.py does a lot on import
with patch('requests.post'), patch('requests.get'), patch('flask.Flask'), patch('flask_sqlalchemy.SQLAlchemy'):
    from app import enrich_product_data

def test_enrich_product_data_no_fallback():
    # Mock data
    product = {'product_id': 'shop_12345', 'product_name': 'Original Name'}
    
    # Mock search results that DO NOT contain 12345
    mock_trending_results = {
        'videos': [
            {'productId': '67890', 'productTitle': 'Trending Product (Woman\'s Bra)'},
            {'productId': '99999', 'productTitle': 'Another Trending Item'}
        ]
    }
    
    with patch('app.fetch_copilot_trending') as mock_fetch:
        mock_fetch.return_value = mock_trending_results
        
        # Call the function
        success, message = enrich_product_data(product, "[TEST]")
        
        print(f"Success: {success}")
        print(f"Message: {message}")
        
        # Assertions
        assert success is False
        assert "No exact PID match found" in message
        assert product['product_name'] == 'Original Name'  # Should not have changed
        print("✅ Verification Passed: No fallback occurred.")

if __name__ == "__main__":
    try:
        test_enrich_product_data_no_fallback()
    except AssertionError as e:
        print(f"❌ Verification Failed: Fallback occurred or unexpected behavior.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error during verification: {e}")
        sys.exit(1)
