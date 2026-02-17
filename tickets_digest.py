import os
import requests

webhook = os.environ["TICKETS_CHAT_WEBHOOK_URL"]

requests.post(webhook, json={"text": "✅ Tickets bot connected successfully"})
