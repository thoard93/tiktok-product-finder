import requests
import json
from datetime import datetime

url = 'https://backend.thedailyvirals.com/api/videos/stats/top-growth-by-date-range'

headers = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'en-US,en;q=0.9',
    'authorization': 'Bearer eyJhbGciOiJIUzI1NiIsImtpZCI6InlNMHVYRXRpWEM3Qm04V0MiLCJ0eXAiOiJKV1QifQ.eyJpc3MiOiJodHRwczovL2h3b3JieG90eHdibnBscW5ob3ZpLnN1cGFiYXNlLmNvL2F1dGgvdjEiLCJzdWIiOiJhMTQyNDUzMi04M2QxLTRiMGItYTcxZS04OGU0ZmQ4MWNkYTgiLCJhdWQiOiJhdXRoZW50aWNhdGVkIiwiZXhwIjoxNzY1Mjk5Nzc1LCJpYXQiOjE3NjUyOTYxNzUsImVtYWlsIjoidGhvYXJkMjAzNUBnbWFpbC5jb20iLCJwaG9uZSI6IiIsImFwcF9tZXRhZGF0YSI6eyJpc19hbm9ueW1vdXMiOmZhbHNlLCJwcm92aWRlciI6ImVtYWlsIiwicHJvdmlkZXJzIjpbImVtYWlsIl19LCJ1c2VyX21ldGFkYXRhIjp7ImVtYWlsX3ZlcmlmaWVkIjp0cnVlLCJmaXJzdE5hbWUiOiJUaG9tYXMiLCJpc19hbm9ueW1vdXMiOmZhbHNlLCJsYXN0TmFtZSI6IkhvYXJkIiwic3RyaXBlQ3VzdG9tZXJJZCI6ImN1c19UUXhMcnc5dGJTSmxNdCIsInVzZXJJZCI6ImExNDI0NTMyLTgzZDEtNGIwYi1hNzFlLTg4ZTRmZDgxY2RhOCJ9LCJyb2xlIjoiYXV0aGVudGljYXRlZCIsImFhbCI6ImFhbDEiLCJhbXIiOlt7Im1ldGhvZCI6InBhc3N3b3JkIiwidGltZXN0YW1wIjoxNzY1Mjk1ODgxfV0sInNlc3Npb25faWQiOiJmZTQ2YTdkMi1kYjk1LTRhNDYtYmFmZi04ZGM3OWVhYTgwZjQiLCJpc19hbm9ueW1vdXMiOmZhbHNlfQ.VWpxeNXqWXDmWFwQyblxgLeW8vYfiG4ZuOsZbBq_oZ4',
    'origin': 'https://www.thedailyvirals.com',
    'referer': 'https://www.thedailyvirals.com/',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36'
}

params = {
    'startDate': '2025-12-08T15:54:51.098Z',
    'endDate': '2025-12-09T15:54:51.098Z',
    'page': '1',
    'limit': '12',
    'sortBy': 'growth',
    'limitedResults': 'false',
    'isPaidPosts': 'true',
    'region': ''
}

try:
    print(f"Sending request to {url}...")
    response = requests.get(url, headers=headers, params=params)
    print(f"Status Code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print("Response Keys:", data.keys())
        if 'videos' in data:
            print(f"Found {len(data['videos'])} videos")
            if len(data['videos']) > 0:
                print("First Video Sample:")
                print(json.dumps(data['videos'][0], indent=2))
        else:
            print("Full Data:", json.dumps(data, indent=2))
    else:
        print("Error Response:", response.text)

except Exception as e:
    print(f"Exception: {e}")
