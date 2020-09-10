from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread

import datetime as dt
import time, logging
import alpaca_trade_api as tradeapi

class BlueprintBot:
    def __init__(self, api_key, api_secret, api_base_url="https://paper-api.alpaca.markets",
                 version='v2', logfile=None, max_workers=200):

        #Setting Logging to both console and a file if logfile is specified
        self.logfile = logfile
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        logFormater = logging.Formatter("%(asctime)s: %(levelname)s: %(message)s")
        consoleHandler = logging.StreamHandler()
        consoleHandler.setFormatter(logFormater)
        logger.addHandler(consoleHandler)
        if logfile:
            fileHandler = logging.FileHandler(logfile, mode='w')
            fileHandler.setFormatter(logFormater)
            logger.addHandler(fileHandler)

        #Alpaca authorize 200 requests per minute and per API key
        #Setting the max_workers for multithreading to 200
        #to go full speed if needed
        self.max_workers = min(max_workers, 200)

        #Connection to alpaca REST API
        self.alpaca = tradeapi.REST(api_key, api_secret, api_base_url, version)

        #getting the account object and setting current positions
        self.positions = self.alpaca.list_positions()
        self.account = self.get_account()

    def update_positions(self):
        """Update the account position informations"""
        positions = self.alpaca.list_positions()
        self.positions = positions
        return positions

    def cancel_buying_orders(self):
        """Cancel all the buying orders with status still open"""
        orders = self.alpaca.list_orders(status="open")
        for order in orders:
            self.alpaca.cancel_order(order.id)

    def is_market_open(self):
        """return True if market is open else false"""
        isOpen = self.alpaca.get_clock().is_open
        return isOpen

    def get_time_to_open(self):
        """Return the remaining time for the market to open in seconds"""
        clock = self.alpaca.get_clock()
        opening_time = clock.next_open.replace(tzinfo=dt.timezone.utc).timestamp()
        curr_time = clock.timestamp.replace(tzinfo=dt.timezone.utc).timestamp()
        time_to_open = opening_time - curr_time
        return time_to_open

    def get_time_to_close(self):
        """Return the remaining time for the market to close in seconds"""
        clock = self.alpaca.get_clock()
        closing_time = clock.next_close.replace(tzinfo=dt.timezone.utc).timestamp()
        curr_time = clock.timestamp.replace(tzinfo=dt.timezone.utc).timestamp()
        time_to_close = closing_time - curr_time
        return time_to_close

    def await_market_to_open(self):
        """Executes infinite loop until market opens"""
        isOpen = self.is_market_open()
        while(not isOpen):
            time_to_open = self.get_time_to_open()
            if time_to_open > 60 * 60:
                delta = dt.timedelta(seconds=time_to_open)
                logging.info("Market will open in %s." % str(delta))
                time.sleep(60 *60)
            elif time_to_open > 60:
                logging.info("%d minutes til market open." % int(time_to_open / 60))
                time.sleep(60)
            else:
                logging.info("%d seconds til market open." % time_to_open)
                time.sleep(time_to_open)

            isOpen = self.is_market_open()

    def get_account(self):
        """Get the account data from the API"""
        account = self.alpaca.get_account()
        return account

    def get_tradable_assets(self):
        """Get the list of all tradable assets from the market"""
        assets = self.alpaca.list_assets()
        assets = [asset for asset in assets if asset.tradable]
        return assets

    def get_last_price(self, symbol):
        """Takes and asset symbol and returns the last known price"""
        bars = self.alpaca.get_barset(symbol, 'minute', 1)
        last_price = bars[symbol][0].c
        return last_price

    def get_last_prices(self, symbols):
        """Takes a list of asset symbols and returns last prices
        in a dictionary"""
        results = {}
        bars = self.alpaca.get_barset(symbols, 'minute', 1)
        for symbol in bars:
            bar = bars[symbol]
            last_value = bar[-1].c if bar else None
            results[symbol] = last_value
        return results

    def get_percentage_changes(self, symbols, time_unity='minute', length=10):
        """Takes a list of asset symbols and returns the variations
        in a dictionary"""
        results = {}
        bars = self.alpaca.get_barset(symbols, time_unity, length)
        for symbol in bars:
            bar = bars[symbol]
            if bar:
                first_value = bar[0].o
                last_value = bar[-1].c
                change = (last_value - first_value) / first_value
                results[symbol] = change
            else:
                results[symbol] = None
        return results

    def submit_order(self, symbol, quantity, side, stop_price_func=None):
        """Submit an order for an asset"""
        if(quantity > 0):
            try:
                stop_loss = {}
                if stop_price_func:
                    last_price = self.get_last_price(symbol)
                    stop_loss['stop_price'] = stop_price_func(last_price)

                kwargs = {
                    'type': 'market',
                    'time_in_force' : 'day'
                }
                if stop_loss: kwargs['stop_loss'] = stop_loss
                self.alpaca.submit_order(symbol, quantity, side, **kwargs)
                logging.info("Market order of | %d %s %s | completed." % (quantity, symbol, side))
                return True
            except Exception as e:
                logging.error("Market order of | %d %s %s | did not go through." % (quantity, symbol, side))
                return False
        else:
            logging.error("Market order of | %d %s %s | not completed" % (quantity, symbol, side))
            return True

    def submit_orders(self, orders):
        """submit orders"""
        all_threads = []
        for order in orders:
            kwargs = {}
            symbol = order.get('symbol')
            quantity = order.get('quantity')
            side = order.get('side')
            stop_price_func = order.get('stop_price_func')
            if stop_price_func: kwargs['stop_price_func'] = stop_price_func

            t = Thread(target=self.submit_order, args=[symbol, quantity, side], kwargs=kwargs)
            t.start()
            all_threads.append(t)

        for t in all_threads:
            t.join()

    def sell_all(self):
        """sell all positions"""
        orders = []
        for position in self.positions:
            order = {
                'symbol': position.symbol,
                'quantity': int(position.qty),
                'side': 'sell'
            }
            orders.append(order)
        self.submit_orders(orders)

    def run(self):
        """This method needs to be overloaded
        by the child bot class"""
        self.cancel_buying_orders()
        self.await_market_to_open()
