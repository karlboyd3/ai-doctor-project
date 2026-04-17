import os
from pyhive import presto
from dotenv import load_dotenv

load_dotenv()

def get_cursor():
    conn = presto.connect(
        host=os.getenv("DB_HOST"),
        port=443,
        username=os.getenv("DB_USER"),
        password=os.getenv("WATSONX_API_KEY"),
        catalog='iceberg_data',
        schema='healthcare',
        protocol='https',
        requests_kwargs={'verify': True}
    )
    return conn.cursor()