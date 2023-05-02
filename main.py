import requests
import sqlite3
import httpx
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import urllib.parse
from forex_python.converter import CurrencyRates
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

app = FastAPI()

# Enable CORS to allow cross-origin requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect to SQLite database
conn = sqlite3.connect("search_history.db")
cursor = conn.cursor()

# Create table if not exists
cursor.execute("""
    CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT,
        time TEXT,
        asin TEXT NOT NULL,
        item_name TEXT,
        price_usd REAL,
        price_uk REAL,
        price_de REAL,
        price_ca REAL
    )
""")
conn.commit()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_search_counts (
        ip TEXT,
        date TEXT,
        count INTEGER,
        PRIMARY KEY (ip, date)
    )
""")
conn.commit()

# Initialize the Jinja2 templates
templates = Jinja2Templates(directory=".")

# Search results data
search_results = []

# Past searches data
past_searches = []

# Maximum number of searches per day per user
max_searches_per_day = 10

# User search count tracker
user_search_counts = {}

# Middleware to track user search counts
@app.middleware("http")
async def track_user_search_counts(request, call_next):
    if request.url.path == "/search" and request.method == "POST":
        ip = request.client.host
        now = datetime.now().date().isoformat()

        cursor.execute("SELECT count FROM user_search_counts WHERE ip = ? AND date = ?", (ip, now))
        row = cursor.fetchone()
        current_count = row[0] if row else 0

        # Check if user has exceeded the maximum number of searches per day
        if current_count >= max_searches_per_day:
            return JSONResponse(content={"error": "Daily searches cap reached. Consider upgrading to the premium service in order to search for more items."}, status_code=429)

        # Update user search count
        if row:
            cursor.execute("UPDATE user_search_counts SET count = count + 1 WHERE ip = ? AND date = ?", (ip, now))
        else:
            cursor.execute("INSERT INTO user_search_counts (ip, date, count) VALUES (?, ?, 1)", (ip, now))

        conn.commit()

    response = await call_next(request)
    return response

headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/112.0"
    }

# Search endpoint
@app.post("/search")
async def search(request_body: dict):
    print("received payload: ", request_body)
    search_item = request_body.get("query")
    if not search_item:
        return JSONResponse(content={"error": "Request body required"}, status_code=400)
    
    # Make request to Amazon.com search page with the given query
    encoded_search_item = urllib.parse.quote(search_item)
    url = f"https://www.amazon.com/s?k={encoded_search_item}"

    response = requests.get(url, headers=headers)
    print("response status code: ", response.status_code)
    print("response content: ", response.text[:100])

    # Parse the HTML response
    soup = BeautifulSoup(response.text, "html.parser")
    if not soup:
        print("Error in Soup parsing")

    # Extract the top 10 product titles, ratings, and ASINs
    results = soup.find_all("div",{"class": "s-result-item"})
    print("results: ", len(results))

    search_results = []
    for result in results[:10]:  # Limit to top 10 results
        title_element = result.find("span", {"class": "a-size-base-plus a-color-base a-text-normal"})
        
        rating_element = result.find("span", {"class": "a-icon-alt"})
        
        price_element = result.find("span", {"class": "a-price-whole"})

        asin_element = result.get("data-asin")

        image_element = result.find("img", {"class": "s-image"})   
        
        if title_element and rating_element and asin_element and price_element and image_element:
            title = title_element.text
            print("Title Element: " + title)
                
            rating = float(rating_element.text.split()[0])
            print("Rating: " + str(rating))

            asin = asin_element
            print("ASIN Element: " + asin_element)

            price_usd = float(price_element.text.replace(",", ""))
            print("Price USD: " + str(price_usd))

            image_link = image_element['src']
            print("Image Link: " + image_link)

            record_id = save_search_to_db(search_item, asin, title, price_usd)
            search_results.append({"record_ud": record_id, "title": title, "rating": rating, "asin": asin, "price_usd":price_usd, "image_link": image_link})
 
    # Return search results as JSON response
    return JSONResponse(content={"results": search_results})

async def fetch_price(url: str, headers: dict) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        if response.status_code == httpx.codes.OK:
            soup = BeautifulSoup(response.text, "html.parser")
            price_element = soup.find("span", {"class": "a-price-whole"})
            if price_element:
                return float(price_element.text.replace(",", ""))
    return None

@app.post("/prices")
async def prices(request_body: dict):
    print("received payload: ", request_body)
    asin = request_body.get("query")
    if not asin:
        return JSONResponse(content={"error": "Request body required"}, status_code=400)
   
    record = find_record_by_asin(asin)
    if not record:
        return JSONResponse(content={"error": "Product not found in database"}, status_code=404)

    #PRICE COMPARISON: Extract prices from Amazon.co.uk, Amazon.de, and Amazon.ca
    prices = []
    url_uk = f"https://www.amazon.co.uk/dp/{asin}"
    url_de = f"https://www.amazon.de/dp/{asin}"
    url_ca = f"https://www.amazon.ca/dp/{asin}"

    price_uk, price_de, price_ca = await asyncio.gather(
            fetch_price(url_uk, headers),
            fetch_price(url_de, headers),
            fetch_price(url_ca, headers),
        )
    
    final_price_uk = convert_to_usd(price_uk, "GBP") if price_uk else "Not Found"
    print("Price in UK: " + str(final_price_uk)) 

    final_price_de= convert_to_usd(price_de, "EUR") if price_de else "Not Found"
    print("Price in DE: " + str(final_price_de))

    final_price_ca = convert_to_usd(price_ca, "CAD") if price_ca else "Not Found"
    print("Price in CA: " + str(final_price_ca))

    # Update the prices in the SQLite database
    update_prices_in_db(record[0], final_price_uk, final_price_de, final_price_ca)

    return JSONResponse(content={"results": {"price_uk": final_price_uk, "price_de":final_price_de, "price_ca": final_price_ca}})

def convert_to_usd(price: float, country: str) -> float:
    c = CurrencyRates()
    date = datetime.now()
    while True:
        try:
            rate = c.get_rate(country, 'USD', date)
            converted_price = price * rate
            return round(converted_price, 2)
        
        except Exception as e:
            date = timedelta(days = 1)
    
# Save search result to the SQLite database
def save_search_to_db(query, asin, item_name, amazon_com_price):
    time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO search_history (query, time, asin, item_name, price_usd)
        VALUES (?, ?, ?, ?, ?)
    """, (query, time, asin, item_name, amazon_com_price))
    conn.commit()
    return cursor.lastrowid

# Function to find a record in the database by ASIN
def find_record_by_asin(asin):
    cursor.execute("""
        SELECT * FROM search_history WHERE asin = ?
    """, (asin,))
    return cursor.fetchone()


def update_prices_in_db(record_id, amazon_co_uk_price, amazon_de_price, amazon_ca_price):
    cursor.execute("""
        UPDATE search_history
        SET price_uk = ?, price_de = ?, price_ca = ?
        WHERE id = ?
    """, (amazon_co_uk_price, amazon_de_price, amazon_ca_price, record_id))
    conn.commit()

  # Function to get search history from the SQLite database
def get_search_history():
    cursor.execute("""
        SELECT * FROM search_history
    """)
    return cursor.fetchall()  

@app.get("/pastsearches")
async def past_searches(request: Request):
    return templates.TemplateResponse("pastsearches.html", {"request": request})


@app.get("/search_history")
async def search_history():
    cursor.execute("SELECT * FROM search_history")
    search_history = [
        {
            "id": row[0],
            "query": row[1],
            "time": row[2],
            "item_name": row[3],
            "amazon_com_price": row[4],
            "amazon_co_uk_price": row[5],
            "amazon_de_price": row[6],
            "amazon_ca_price": row[7],
        }
        for row in cursor.fetchall()
    ]
    return search_history
