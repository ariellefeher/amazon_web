import asyncio
import requests
import sqlite3
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import urllib.parse
from forex_python.converter import CurrencyRates
import aiofiles
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

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
    now = datetime.now().date().isoformat()
    # print("request: ", request)
    if request.url.path == "/search" and request.method == "POST":
        ip = request.client.host

        if ip not in user_search_counts:
            user_search_counts[ip] = {now: 0}

        elif now not in user_search_counts[ip]:
            user_search_counts[ip][now] = 0

        user_search_counts[ip][now] += 1

        # Check if user has exceeded the maximum number of searches per day
        if user_search_counts[ip][now] > max_searches_per_day:
            return JSONResponse(content={"error": "Search limit exceeded for today"}, status_code=429)

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
    
        # # DEBUG:  work on mockup html data to avoid sending too many request to Amazon 
        # response_mockup_path = "/Users/ariel/Downloads/mockup.txt"
        # with open(response_mockup_path, "w") as fh:
        #     fh.write(response.text)

        # response_text_mockup = ""
        # with open(response_mockup_path, "r") as fh:
        #     response_text_mockup = fh.read()
        
        # soup = BeautifulSoup(response_text_mockup, "html.parser")

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
    print(type(search_results))
    print(search_results)
    return JSONResponse(content={"results": search_results})
    # return JSONResponse(content=search_results)

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
    # url_usa =  f"https://www.amazon.com/dp/{asin}"      
    url_uk = f"https://www.amazon.co.uk/dp/{asin}"
    url_de = f"https://www.amazon.de/dp/{asin}"
    url_ca = f"https://www.amazon.ca/dp/{asin}"

    # response_usa = requests.get(url_usa, headers=headers)
    response_uk = requests.get(url_uk, headers=headers)
    response_de = requests.get(url_de, headers=headers)
    response_ca = requests.get(url_ca, headers=headers)

    # print("USA response status code: ", response_usa.status_code)
    print("UK response status code: ", response_uk.status_code)
    print("DE response status code: ", response_de.status_code)
    print("CA response status code: ", response_ca.status_code)

    # soup_usa = BeautifulSoup(response_usa.text, "html.parser")
    soup_uk = BeautifulSoup(response_uk.text, "html.parser")
    soup_de = BeautifulSoup(response_de.text, "html.parser")
    soup_ca = BeautifulSoup(response_ca.text, "html.parser")

    # amazon_usa_element = soup_usa.find("span", {"class": "a-price-whole"})
    amazon_uk_element = soup_uk.find("span", {"class": "a-price-whole"})
    amazon_de_element = soup_de.find("span", {"class": "a-price-whole"})
    amazon_ca_element = soup_ca.find("span", {"class": "a-price-whole"})
            
    # price_usa = float(amazon_usa_element.text.replace(",", "")) if amazon_usa_element else None
    price_uk = float(amazon_uk_element.text.replace(",", "")) if amazon_uk_element else None
    price_de = float(amazon_de_element.text.replace(",", "")) if amazon_de_element else None
    price_ca = float(amazon_ca_element.text.replace(",", "")) if amazon_ca_element else None

    # print("Price in US: " + str(price_usa))

    final_price_uk = convert_to_usd(price_uk, "GBP") if price_uk else "Not Found"
    print("Price in UK: " + str(final_price_uk)) 

    final_price_de= convert_to_usd(price_de, "EUR") if price_de else "Not Found"
    print("Price in DE: " + str(final_price_de))

    final_price_ca = convert_to_usd(price_ca, "CAD") if price_ca else "Not Found"
    print("Price in CA: " + str(final_price_ca))

        # Update the prices in the SQLite database
    update_prices_in_db(record[0], final_price_uk, final_price_de, final_price_ca)

    # prices.append({"price_uk": final_price_uk, "price_de":final_price_de, "price_ca": final_price_ca})
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

    # cursor.execute("SELECT * FROM search_history")
    # search_history = cursor.fetchall()
    # search_history_data = [
    #     {
    #         "id": record[0],
    #         "query": record[1],
    #         "time": record[2],
    #         "asin": record[3],
    #         "item_name": record[4],
    #         "amazon_com_price": record[5],
    #         "amazon_co_uk_price": record[6],
    #         "amazon_de_price": record[7],
    #         "amazon_ca_price": record[8],
    #     }
    #     for record in search_history
    # ]
    # print(search_history_data)  # Add this line to print the data
    # return templates.TemplateResponse("pastsearches.html", {"request": request, "search_history": search_history_data})

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

# @app.get("/pastsearches", response_class=HTMLResponse)
# async def past_searches(request: Request):
#     search_history = get_search_history()
#     return templates.TemplateResponse("pastsearches.html", {"request": request, "search_history": search_history})


