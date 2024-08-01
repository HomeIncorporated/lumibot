# This file contains helper functions for getting data from Polygon.io
import logging
import time
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal
import requests
from lumibot import LUMIBOT_CACHE_FOLDER
from lumibot.entities import Asset
from thetadata import ThetaClient
import holidays

WAIT_TIME = 60
MAX_DAYS = 30
THETA_TIME_SHIFT = 4
CACHE_SUBFOLDER = "thetadata"
BASE_URL = "http://127.0.0.1:25510"


def get_price_data(
    username: str,
    password: str,
    asset: Asset,
    start: datetime,
    end: datetime,
    timespan: str = "minute",
    quote_asset: Asset = None,
    dt=None
):
    """
    Queries ThetaData for pricing data for the given asset and returns a DataFrame with the data. Data will be
    cached in the LUMIBOT_CACHE_FOLDER/{CACHE_SUBFOLDER} folder so that it can be reused later and we don't have to query
    ThetaData every time we run a backtest.

    Parameters
    ----------
    username : str
        Your ThetaData username
    password : str
        Your ThetaData password
    asset : Asset
        The asset we are getting data for
    start : datetime
        The start date/time for the data we want
    end : datetime
        The end date/time for the data we want
    timespan : str
        The timespan for the data we want. Default is "minute" but can also be "second", "hour", "day", "week",
        "month", "quarter"
    quote_asset : Asset
        The quote asset for the asset we are getting data for. This is only needed for Forex assets.

    Returns
    -------
    pd.DataFrame
        A DataFrame with the pricing data for the asset

    """
    if start.date() < dt.date():
        start = dt
    # Check if we already have data for this asset in the feather file
    df_all = None
    df_feather = None
    cache_file = build_cache_filename(asset, timespan)
    if cache_file.exists():
        logging.info(
            f"\nLoading pricing data for {asset} / {quote_asset} with '{timespan}' timespan from cache file...")
        df_feather = load_cache(cache_file)
        df_all = df_feather.copy()  # Make a copy so we can check the original later for differences

    # Check if we need to get more data
    missing_dates = get_missing_dates(df_all, asset, start, end)
    if not missing_dates:
        df_all.index = df_all.index + pd.Timedelta(hours=THETA_TIME_SHIFT)
        return df_all

    logging.info(
        f"\nGetting pricing data for {asset} / {quote_asset} with '{timespan}' timespan directly from ThetaData datastream...")

    start = missing_dates[0]  # Data will start at 8am UTC (4am EST)
    end = missing_dates[-1]  # Data will end at 23:59 UTC (7:59pm EST)
    delta = timedelta(days=MAX_DAYS)

    interval_ms = None
    # Calculate the interval in milliseconds
    if timespan == "second":
        interval_ms = 1000
    elif timespan == "minute":
        interval_ms = 60000
    elif timespan == "hour":
        interval_ms = 3600000
    elif timespan == "day":
        interval_ms = 86400000
    else:
        interval_ms = 60000
        logging.warning(f"Unsupported timespan: {timespan}, using default of 1 minute")

    while start <= missing_dates[-1]:
        # If we don't have a paid subscription, we need to wait 1 minute between requests because of
        # the rate limit. Wait every other query so that we don't spend too much time waiting.

        if end > start + delta:
            end = start + delta

        result_df = get_historical_data(asset, start, end, interval_ms, username, password)

        if result_df is None or len(result_df) == 0:
            logging.warning(
                f"No data returned for {asset} / {quote_asset} with '{timespan}' timespan between {start} and {end}"
            )

        else:
            df_all = update_df(df_all, result_df)
            print(f"\ndf_all head: \n{df_all.head()}")

        start = end + timedelta(days=1)
        end = start + delta

        if asset.expiration and start > asset.expiration:
            break

    update_cache(cache_file, df_all, df_feather)
    df_all.index = df_all.index + pd.Timedelta(hours=THETA_TIME_SHIFT)
    return df_all


def get_trading_dates(asset: Asset, start: datetime, end: datetime):
    """
    Get a list of trading days for the asset between the start and end dates
    Parameters
    ----------
    asset : Asset
        Asset we are getting data for
    start : datetime
        Start date for the data requested
    end : datetime
        End date for the data requested

    Returns
    -------

    """
    # Crypto Asset Calendar
    if asset.asset_type == "crypto":
        # Crypto trades every day, 24/7 so we don't need to check the calendar
        return [start.date() + timedelta(days=x) for x in range((end.date() - start.date()).days + 1)]

    # Stock/Option Asset for Backtesting - Assuming NYSE trading days
    elif asset.asset_type == "stock" or asset.asset_type == "option":
        cal = mcal.get_calendar("NYSE")

    # Forex Asset for Backtesting - Forex trades weekdays, 24hrs starting Sunday 5pm EST
    # Calendar: "CME_FX"
    elif asset.asset_type == "forex":
        cal = mcal.get_calendar("CME_FX")

    else:
        raise ValueError(f"Unsupported asset type for thetadata: {asset.asset_type}")

    # Get the trading days between the start and end dates
    df = cal.schedule(start_date=start.date(), end_date=end.date())
    trading_days = df.index.date.tolist()
    return trading_days


def build_cache_filename(asset: Asset, timespan: str):
    """Helper function to create the cache filename for a given asset and timespan"""

    lumibot_cache_folder = Path(LUMIBOT_CACHE_FOLDER) / CACHE_SUBFOLDER

    # If It's an option then also add the expiration date, strike price and right to the filename
    if asset.asset_type == "option":
        if asset.expiration is None:
            raise ValueError(f"Expiration date is required for option {asset} but it is None")

        # Make asset.expiration datetime into a string like "YYMMDD"
        expiry_string = asset.expiration.strftime("%y%m%d")
        uniq_str = f"{asset.symbol}_{expiry_string}_{asset.strike}_{asset.right}"
    else:
        uniq_str = asset.symbol

    cache_filename = f"{asset.asset_type}_{uniq_str}_{timespan}.feather"
    cache_file = lumibot_cache_folder / cache_filename
    return cache_file


def get_missing_dates(df_all, asset, start, end):
    """
    Check if we have data for the full range
    Later Query to Polygon will pad an extra full day to start/end dates so that there should never
    be any gap with intraday data missing.

    Parameters
    ----------
    df_all : pd.DataFrame
        Data loaded from the cache file
    asset : Asset
        Asset we are getting data for
    start : datetime
        Start date for the data requested
    end : datetime
        End date for the data requested

    Returns
    -------
    list[datetime.date]
        A list of dates that we need to get data for
    """
    trading_dates = get_trading_dates(asset, start, end)
    if df_all is None or not len(df_all):
        return trading_dates

    # It is possible to have full day gap in the data if previous queries were far apart
    # Example: Query for 8/1/2023, then 8/31/2023, then 8/7/2023
    # Whole days are easy to check for because we can just check the dates in the index
    dates = pd.Series(df_all.index.date).unique()
    missing_dates = sorted(set(trading_dates) - set(dates))

    # For Options, don't need any dates passed the expiration date
    if asset.asset_type == "option":
        missing_dates = [x for x in missing_dates if x <= asset.expiration]

    return missing_dates


def load_cache(cache_file):
    """Load the data from the cache file and return a DataFrame with a DateTimeIndex"""
    df_feather = pd.read_feather(cache_file)

    # Set the 'datetime' column as the index of the DataFrame
    df_feather.set_index("datetime", inplace=True)

    df_feather.index = pd.to_datetime(
        df_feather.index
    )  # TODO: Is there some way to speed this up? It takes several times longer than just reading the feather file
    df_feather = df_feather.sort_index()

    # Check if the index is already timezone aware
    if df_feather.index.tzinfo is None:
        # Set the timezone to New York
        df_feather.index = df_feather.index.tz_localize("America/New_York")

    return df_feather


def update_cache(cache_file, df_all, df_feather):
    """Update the cache file with the new data"""
    # Check if df_all is different from df_feather (if df_feather exists)
    if df_all is not None and len(df_all) > 0:
        # Check if the dataframes are the same
        if df_all.equals(df_feather):
            return

        # Create the directory if it doesn't exist
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        # Reset the index to convert DatetimeIndex to a regular column
        df_all_reset = df_all.reset_index()

        # Save the data to a feather file
        df_all_reset.to_feather(cache_file)


def update_df(df_all, result):
    """
    Update the DataFrame with the new data from ThetaData

    Parameters
    ----------
    df_all : pd.DataFrame
        A DataFrame with the data we already have
    result : list
        A List of dictionaries with the new data from Polygon
        Format: [{'o': 1.0, 'h': 2.0, 'l': 3.0, 'c': 4.0, 'v': 5.0, 't': 116120000000}]
    """

    df = pd.DataFrame(result)
    if not df.empty:
        df = df.set_index("datetime").sort_index()
        if df_all is not None:
            print(f"\n new loop df_all head: \n{df_all.head()}")
            # set "datetime" column as index of df_all
            df_all = df_all.set_index("datetime").sort_index()
            print(f"\n after setting index df_all head: \n{df_all.head()}")
        print(f"\nbefore utc: df head: \n{df.head()}")
        df.index = df.index.tz_localize("UTC")
        print(f"\nafter utc: df head: \n{df.head()}")
        if df_all is None or df_all.empty:
            df_all = df
            print(f"\nNo df_all, assign df_all=df")
        else:
            print(f"\nInside update_df, df_all head: \n{df_all.head()}")
            print(f"\nInside update_df, df head: \n{df.head()}")
            df_all = pd.concat([df_all, df]).sort_index()
            df_all = df_all[~df_all.index.duplicated(keep="first")]  # Remove any duplicate rows

            print(f"\nafter concat df_all head: \n{df_all.head()}")
        # df_all = df_all.reset_index()
        print(f"\nafter concat reset index df_all head: \n{df_all.head()}")

    return df_all


def start_theta_data_client(username: str, password: str):
    # First try shutting down any existing connection
    try:
        requests.get(f"{BASE_URL}/v2/system/terminal/shutdown")
    except Exception:
        pass

    client = ThetaClient(username=username, passwd=password)

    time.sleep(1)

    return client


def check_connection(username: str, password: str):
    # Do endless while loop and check if connected every 100 milliseconds
    MAX_RETRIES = 15
    counter = 0
    client = None
    while True:
        try:
            time.sleep(0.5)
            res = requests.get(f"{BASE_URL}/v2/system/mdds/status", timeout=1)
            con_text = res.text

            if con_text == "CONNECTED":
                logging.debug("Connected to Theta Data!")
                break
            elif con_text == "DISCONNECTED":
                logging.debug("Disconnected from Theta Data!")
                counter += 1
            else:
                logging.info(f"Unknown connection status: {con_text}, starting theta data client")
                client = start_theta_data_client(username=username, password=password)
                counter += 1
        except Exception as e:
            client = start_theta_data_client(username=username, password=password)
            counter += 1

        if counter > MAX_RETRIES:
            logging.error("Cannot connect to Theta Data!")
            break

    return client


def get_all_holidays(year, country='US'):
    """
    Get a list of all holidays for a given year and country.

    :param year: Integer, the year for which to get the holidays
    :param country: String, the country for which to get the holidays
    :return: List of tuples (date, name) representing holidays
    """
    country_holidays = holidays.country_holidays(country, years=year)
    all_holidays = [date for date, name in country_holidays.items()]
    return all_holidays


def is_weekend(date):
    """
    Check if the given date is a weekend.

    :param date: datetime.date object
    :return: Boolean, True if weekend, False otherwise
    """
    return date.weekday() >= 5  # 5 = Saturday, 6 = Sunday


def get_request(url: str, headers: dict, querystring: dict, username: str, password: str):
    counter = 0
    print(f"querystring: {querystring}")
    expiry = querystring['exp'] if 'exp' in querystring else None

    # Check if expiry date is a holiday or weekend, this part of the logic is currently
    # only for options mode
    if expiry:
        expiry = datetime.strptime(expiry, "%Y%m%d")
        holidays = get_all_holidays(expiry.year)
        if expiry in holidays:
            logging.info(f"\nSKIP: Expiry {expiry} date is a holiday!")
            return None
        if is_weekend(expiry):
            logging.info(f"\nSKIP: Expiry {expiry} date is a weekend!")
            return None

    while True:
        try:
            response = requests.get(url, headers=headers, params=querystring)
            # If status code is not 200, then we are not connected
            if response.status_code != 200:
                check_connection(username=username, password=password)
            else:
                json_resp = response.json()

                # Check if json_resp has error_type inside of header
                if "error_type" in json_resp["header"] and json_resp["header"]["error_type"] != "null":
                    logging.error(
                        f"Error getting data from Theta Data: {json_resp['header']['error_type']},\nquerystring: {querystring}")
                    check_connection(username=username, password=password)
                else:
                    print(f"\nthe_helper: Found valid querystring: {querystring}")
                    break

        except Exception as e:
            check_connection(username=username, password=password)

        counter += 1
        if counter > 1:
            raise ValueError("Cannot connect to Theta Data!")

    return json_resp


def get_historical_data(asset: Asset, start_dt: datetime, end_dt: datetime, ivl: int, username: str, password: str):
    """
    Get data from ThetaData

    Parameters
    ----------
    asset : Asset
        The asset we are getting data for
    start_dt : datetime
        The start date/time for the data we want
    end_dt : datetime
        The end date/time for the data we want
    ivl : int
        The interval for the data we want in milliseconds (eg. 60000 for 1 minute)
    username : str
        Your ThetaData username
    password : str
        Your ThetaData password

    Returns
    -------
    pd.DataFrame
        A DataFrame with the data for the asset
    """

    # Comvert start and end dates to strings
    start_date = start_dt.strftime("%Y%m%d")
    end_date = end_dt.strftime("%Y%m%d")

    # Create the url based on the asset type
    if asset.asset_type == "stock":
        url = f"{BASE_URL}/hist/stock/ohlc"
        querystring = {"root": asset.symbol, "start_date": start_date, "end_date": end_date, "ivl": ivl}
    elif asset.asset_type == "option":
        url = f"{BASE_URL}/hist/option/ohlc"

        # Convert the expiration date to a string
        expiration_str = asset.expiration.strftime("%Y%m%d")

        # Convert the strike price to an integer and multiply by 1000
        strike = int(asset.strike * 1000)

        querystring = {
            "root": asset.symbol,
            "start_date": start_date,
            "end_date": end_date,
            "ivl": ivl,
            "strike": strike,  # "140000",
            "exp": expiration_str,  # "20220930",
            "right": "C" if asset.right == "CALL" else "P",
        }

    headers = {"Accept": "application/json"}

    # Send the request

    json_resp = get_request(url=url, headers=headers, querystring=querystring,
                            username=username, password=password)
    if json_resp is None:
        return None

    # Convert to pandas dataframe
    df = pd.DataFrame(json_resp["response"], columns=json_resp["header"]["format"])

    # Remove any rows where count is 0 (no data - the prices will be 0 at these times too)
    df = df[df["count"] != 0]

    # Function to combine ms_of_day and date into datetime
    def combine_datetime(row):
        # Ensure the date is in integer format and then convert to string
        date_str = str(int(row["date"]))
        base_date = datetime.strptime(date_str, "%Y%m%d")
        # Adding the milliseconds of the day to the base date
        datetime_value = base_date + timedelta(milliseconds=int(row["ms_of_day"]))
        return datetime_value

    # Apply the function to each row to create a new datetime column
    df["datetime"] = df.apply(combine_datetime, axis=1)

    # Convert the datetime column to a datetime
    df["datetime"] = pd.to_datetime(df["datetime"])

    # Drop the ms_of_day and date columns
    df = df.drop(columns=["ms_of_day", "date"])

    return df


def get_expirations(username: str, password: str, ticker: str, after_date: date):
    """
    Get a list of expiration dates for the given ticker

    Parameters
    ----------
    username : str
        Your ThetaData username
    password : str
        Your ThetaData password
    ticker : str
        The ticker for the asset we are getting data for

    Returns
    -------
    list[str]
        A list of expiration dates for the given ticker
    """
    # Create the url based on the request type
    url = f"{BASE_URL}/list/expirations"

    querystring = {"root": ticker}

    headers = {"Accept": "application/json"}

    # Send the request
    json_resp = get_request(url=url, headers=headers, querystring=querystring, username=username, password=password)

    # Convert to pandas dataframe
    df = pd.DataFrame(json_resp["response"], columns=json_resp["header"]["format"])

    # Convert df to a list of the first (and only) column
    expirations = df.iloc[:, 0].tolist()

    # Convert after_date to a number
    after_date_int = int(after_date.strftime("%Y%m%d"))

    # Filter out any dates before after_date
    expirations = [x for x in expirations if x >= after_date_int]

    # Convert from "YYYYMMDD" (an int) to "YYYY-MM-DD" (a string)
    expirations_final = []
    for expiration in expirations:
        expiration_str = str(expiration)
        # Add the dashes to the string
        expiration_str = f"{expiration_str[:4]}-{expiration_str[4:6]}-{expiration_str[6:]}"
        # Add the string to the list
        expirations_final.append(expiration_str)

    return expirations_final


def get_strikes(username: str, password: str, ticker: str, expiration: datetime):
    """
    Get a list of strike prices for the given ticker and expiration date

    Parameters
    ----------
    username : str
        Your ThetaData username
    password : str
        Your ThetaData password
    ticker : str
        The ticker for the asset we are getting data for
    expiration : date
        The expiration date for the options we want

    Returns
    -------
    list[float]
        A list of strike prices for the given ticker and expiration date
    """
    # Create the url based on the request type
    url = f"{BASE_URL}/list/strikes"

    # Convert the expiration date to a string
    expiration_str = expiration.strftime("%Y%m%d")

    querystring = {"root": ticker, "exp": expiration_str}

    headers = {"Accept": "application/json"}

    # Send the request
    json_resp = get_request(url=url, headers=headers, querystring=querystring, username=username, password=password)

    # Convert to pandas dataframe
    df = pd.DataFrame(json_resp["response"], columns=json_resp["header"]["format"])

    # Convert df to a list of the first (and only) column
    strikes = df.iloc[:, 0].tolist()

    # Divide each strike by 1000 to get the actual strike price
    strikes = [x / 1000 for x in strikes]

    return strikes
