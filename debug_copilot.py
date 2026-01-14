import requests
import json
import os
import re

# Use the cookie from test_copilot.py
COOKIE_STR = r'''_ga=GA1.1.203608145.1767626398; __clerk_db_jwt_oNnsQakD=dvb_37qMxQlwWSisbxXvehFgH9uKiOD; __refresh_oNnsQakD=KCSvzhzk0RSn1EtQpmeT; __client_uat_oNnsQakD=0; __client_uat=1767831328; __client_uat_pOM46XQh=1767831328; __refresh_pOM46XQh=7Vf54AUqKWbjr4pvngRv; _ga_QQHYFR2Z45=GS2.1.s1768421860$o18$g0$t1768421860$j60$l0$h0; clerk_active_context=sess_37x4KyiW279bLUgbI14LCl7x7MD:; __session=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zN3gwZTJ0SXEweFhGRHpabVUzYUY2YzlmREsiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2ODQyMTkyMSwiZnZhIjpbOTg0MiwtMV0sImlhdCI6MTc2ODQyMTg2MSwiaXNzIjoiaHR0cHM6Ly9jbGVyay50aWt0b2tjb3BpbG90LmNvbSIsIm5iZiI6MTc2ODQyMTg1MSwic2lkIjoic2Vzc18zN3g0S3lpVzI3OWJMVWdiSTE0TENsN3g3TUQiLCJzdHMiOiJhY3RpdmUiLCJzdWIiOiJ1c2VyXzM3eDRMNDNWQnpSS0lBbmwzT3ZZY0ZiTVVTbSIsInYiOjJ9.W4wz4zDro3SySkijepOzsN0MABxspSPfB-SgStfdELo2V_p3WnISGUpWXgWOv1THrZKSW9dqwyz7bTTzz43waZ2Dt-sDh4Ilu9S74kebBHtWwWvDU0r595KqXeHxohQlVTyzke1bvZ4ecGIiaVFj93sF36r_NdntVNL_nM-9Rd0HPexveS0OPh9edxKMz8C8ypFGqF-HnXZ2hnF2p_FNbb2DlFrNq507AquAu90jC57So3fYcYRfqi7ZacRn2Tjep4Ceoy4eqJpwf3NweNb588vQZpgmbSeeAhiOmduLTlJSMBpjfIQIoup04HhVMIt17GZ-_fSTCL97BsYFLjESvQ; __session_pOM46XQh=eyJhbGciOiJSUzI1NiIsImNhdCI6ImNsX0I3ZDRQRDExMUFBQSIsImtpZCI6Imluc18zN3gwZTJ0SXEweFhGRHpabVUzYUY2YzlmREsiLCJ0eXAiOiJKV1QifQ.eyJhenAiOiJodHRwczovL3d3dy50aWt0b2tjb3BpbG90LmNvbSIsImV4cCI6MTc2ODQyMTkyMSwiZnZhIjpbOTg0MiwtMV0sImlhdCI6MTc2ODQyMTg2MSwiaXNzIjoiaHR0cHM6Ly9jbGVyay50aWt0b2tjb3BpbG90LmNvbSIsIm5iZiI6MTc2ODQyMTg1MSwic2lkIjoic2Vzc18zN3g0S3lpVzI3OWJMVWdiSTE0TENsN3g3TUQiLCJzdHMiOiJhY3RpdmUiLCJzdWIiOiJ1c2VyXzM3eDRMNDNWQnpSS0lBbmwzT3ZZY0ZiTVVTbSIsInYiOjJ9.W4wz4zDro3SySkijepOzsN0MABxspSPfB-SgStfdELo2V_p3WnISGUpWXgWOv1THrZKSW9dqwyz7bTTzz43waZ2Dt-sDh4Ilu9S74kebBHtWwWvDU0r595KqXeHxohQlVTyzke1bvZ4ecGIiaVFj93sF36r_NdntVNL_nM-9Rd0HPexveS0OPh9edxKMz8C8ypFGqF-HnXZ2hnF2p_FNbb2DlFrNq507AquAu90jC57So3fYcYRfqi7ZacRn2Tjep4Ceoy4eqJpwf3NweNb588vQZpgmbSeeAhiOmduLTlJSMBpjfIQIoup04HhVMIt17GZ-_fSTCL97BsYFLjESvQ; ph_phc_RA3Nibqho9D4F0xdDth2UnvdOUkcX3oenoWWDkeVnow_posthog=%7B%22distinct_id%22%3A%22user_37x4L43VBzRKIAnl3OvYcFbMUSm%22%2C%22%24sesid%22%3A%5B1768421871931%2C%22019bbe28-25dc-74cf-ac2a-c12549f8dd2a%22%2C1768421860811%5D%2C%22%24epp%22%3Atrue%2C%22%24initial_person_info%22%3A%7B%22r%22%3A%22%24direct%22%2C%22u%22%3A%22https%3A%2F%2Fwww.tiktokcopilot.com%2Fsign-in%3Fredirect_url%3Dhttps%253A%252F%252Fwww.tiktokcopilot.com%252F%22%7D%7D'''

def test_search(product_id):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": COOKIE_STR,
        "Referer": "https://www.tiktokcopilot.com/",
    }
    
    # Try Stage 1: productId param
    params = {
        "limit": 5,
        "region": "US",
        "sAggMode": "net",
        "searchType": "product",
        "productId": product_id,
        "keywords": product_id
    }
    
    print(f"Testing search for {product_id}...")
    res = requests.get("https://www.tiktokcopilot.com/api/trending", headers=headers, params=params, timeout=30)
    print(f"Status: {res.status_code}")
    if res.status_code == 200:
        data = res.json()
        videos = data.get('videos', [])
        print(f"Found {len(videos)} results")
        for v in videos:
            print(f" - {v.get('productId')}: {v.get('productTitle')}")
    else:
        print(f"Error: {res.text}")

if __name__ == "__main__":
    # Test with a known trending product ID if possible, or the one from the user's link
    # User's link: https://www.tiktok.com/t/ZPHo6nheVVvFW-73NX1/
    # If I don't have the ID yet, I'll use the placeholder
    test_search("1729437146522941575")
