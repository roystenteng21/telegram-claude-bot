import caldav

ICLOUD_USERNAME = "roysten.teng@outlook.com"  # replace this
ICLOUD_PASSWORD = "zgmf-zmyk-giem-ypgj"             # replace with app-specific password

try:
    client = caldav.DAVClient(
        url="https://caldav.icloud.com",
        username=ICLOUD_USERNAME,
        password=ICLOUD_PASSWORD
    )
    principal = client.principal()
    calendars = principal.calendars()
    print("✅ Connected successfully. Calendars found:")
    for cal in calendars:
        print(f"  - {cal.name}")
except Exception as e:
    print(f"❌ Connection failed: {e}")
