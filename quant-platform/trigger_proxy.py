import requests
print(requests.post('http://127.0.0.1:8081/api/sync/intra', json={'freq':'5m', 'days':1}).json())
