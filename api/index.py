import requests
import re
import os
import time
from datetime import datetime

# === ENVIRONMENT VARIABLES ===
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN") 
TELEGRAM_CHANNEL_ID = os.environ.get("CHANNEL_ID")
IVA_COOKIES = {
    "sessionid": os.environ.get("IVA_SESSION", ""),
    "csrftoken": os.environ.get("IVA_CSRF", "")
}
OWNER_NAME = "Zain Malik"

seen_messages = set()

COUNTRY_FLAGS = {
    "pakistan": "🇵🇰", "india": "🇮🇳", "usa": "🇺🇸", "uk": "🇬🇧",
    "germany": "🇩🇪", "france": "🇫🇷", "ivory": "🇨🇮", "cote": "🇨🇮",
    "nigeria": "🇳🇬", "bangladesh": "🇧🇩", "turkey": "🇹🇷",
    "default": "🌍"
}

SERVICE_ICONS = {
    "whatsapp": "🟢", "facebook": "🔵", "google": "🔴",
    "telegram": "✈️", "instagram": "🟣", "default": "📱"
}

def get_country_flag(text):
    text_lower = text.lower()
    for country, flag in COUNTRY_FLAGS.items():
        if country in text_lower:
            return flag, country.title()
    return COUNTRY_FLAGS["default"], "Unknown"

def get_service_icon(text):
    text_lower = text.lower()
    for service, icon in SERVICE_ICONS.items():
        if service in text_lower:
            return icon, service.title()
    return SERVICE_ICONS["default"], "SMS"

def extract_otp(text):
    patterns = [r'\b\d{6}\b', r'\b\d{5}\b', r'\b\d{4}\b']
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)
    return "000000"

def mask_number(phone):
    if len(phone) > 8:
        return phone[:4] + "*****" + phone[-4:]
    return phone

def send_to_channel(phone, sender, message, timestamp):
    country_flag, country_name = get_country_flag(message + " " + sender)
    service_icon, service_name = get_service_icon(message + " " + sender)
    otp = extract_otp(message)
    
    text = f"""{country_flag} <b>New {country_name} {service_name} OTP!</b>

🕐 <b>Time:</b> <code>{timestamp}</code>
{country_flag} <b>Country:</b> {country_name}
{service_icon} <b>Service:</b> {service_name}
📞 <b>Number:</b> <code>{mask_number(phone)}</code>
🔑 <b>OTP:</b> <code>{otp}</code>
✉️ <b>Full Message:</b>
<code>{message}</code>
👤 <b>Owner:</b> {OWNER_NAME}"""
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def fetch_sms():
    global seen_messages
    try:
        today = datetime.now().strftime("%d/%m/%Y")
        endpoints = ["/api/sms", "/portal/api/sms", "/api/messages", "/portal/live/api/sms"]
        
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "X-CSRF-TOKEN": IVA_COOKIES.get("csrftoken", ""),
        }
        
        for endpoint in endpoints:
            try:
                url = f"https://www.ivasms.com{endpoint}"
                response = requests.get(url, headers=headers, cookies=IVA_COOKIES, 
                                      params={"date": today, "limit": 20}, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    messages = data.get("messages", []) or data.get("sms", []) or data.get("data", []) or []
                    
                    for msg in messages:
                        text = msg.get("text", "")
                        time_str = msg.get("time", datetime.now().strftime("%H:%M:%S"))
                        msg_id = text + time_str
                        
                        if msg_id not in seen_messages:
                            phone = msg.get("phone", "Unknown")
                            sender = msg.get("sender", "Unknown")
                            send_to_channel(phone, sender, text, time_str)
                            seen_messages.add(msg_id)
                    
                    if len(seen_messages) > 200:
                        seen_messages.clear()
                    return
            except:
                continue
    except:
        pass

if __name__ == "__main__":
    print("🚀 Bot is LIVE on AWS Server!")
    print("✅ Checking for new OTPs every 2 seconds...")
    while True:
        fetch_sms()
        time.sleep(2) # 2 Second ki superfast speed!
                                                  
