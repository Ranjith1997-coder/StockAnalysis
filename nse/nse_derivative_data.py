
import pandas as pd
import datetime as dt
import zipfile
from io import BytesIO
from nse.nse_utils import *
from nse.nse_constants import *
import common.shared as shared
from typing import Optional

from common.logging_util import logger

class NSE_DATA_CLASS:

    @staticmethod
    def get_live_futures_and_options_data_intraday(stock : str, currentExpiry: str, nextExpiry: str):
        try :
            expiry = currentExpiry
            date_obj = datetime.strptime(expiry, "%d-%b-%Y")
            expiry = date_obj.strftime("%d-%m-%Y")
            data = NSE_DATA_CLASS.get_futures_and_options_data_from_nse_intraday(stock, currentExpiry)
        except Exception:
            logger.error("Error while getting the futures and options data")
            raise Exception()
        res = { 
                "futuresData" : {"currExpiry" : None, "nextExpiry" : None} , 
                "optionsData": {"currExpiry" : None, "nextExpiry" : None} 
            }
        for derivative_data in data["stocks"] : 
            if (derivative_data["metadata"]["instrumentType"] == "Stock Futures") and \
                derivative_data["metadata"]["expiryDate"] == currentExpiry:
                res["futuresData"]["currExpiry"] = NSE_DATA_CLASS.parseFuturesData(stock, derivative_data)
            elif (derivative_data["metadata"]["instrumentType"] == "Stock Futures") and \
                derivative_data["metadata"]["expiryDate"] == nextExpiry:
                res["futuresData"]["nextExpiry"] = NSE_DATA_CLASS.parseFuturesData(stock, derivative_data)
    
        return res
    
    @staticmethod
    def parseFuturesData(stockSymbol: str, futuresData : dict) -> pd.DataFrame : 
        futures_dict = {}
        futures_dict['TIMESTAMP'] = floor_to_5_min()
        futures_dict['SYMBOL'] = stockSymbol
        futures_dict['EXPIRY_DT'] = futuresData["metadata"]['expiryDate']
        futures_dict['LAST_TRADED_PRICE'] = futuresData["metadata"]['lastPrice']
        futures_dict['VOLUME'] = futuresData["metadata"]['numberOfContractsTraded']
        futures_dict['OPEN_INT'] = futuresData["marketDeptOrderBook"]["tradeInfo"]['openInterest']

        futures_df = pd.DataFrame([futures_dict], columns=future_price_volume_data_column_intraday)
        futures_df['TIMESTAMP'] = pd.to_datetime(futures_df['TIMESTAMP'])
        futures_df.set_index('TIMESTAMP', inplace=True)
        
        return futures_df

    @staticmethod
    def get_futures_and_options_data_from_nse_intraday(symbol : str, expiry: str) -> dict:
        origin_url = "https://www.nseindia.com/get-quotes/derivatives"
        url = "https://www.nseindia.com/api/quote-derivative?"
        payload = f"symbol={symbol}&identifier=FUTSTK{symbol}{expiry}XX0.00"
        try:
            data_dict = nse_urlfetch(url + payload, origin_url=origin_url).json()
        except Exception as e:
            raise ValueError(f" Invalid parameters : NSE error:{e}")
        return data_dict


    @staticmethod
    def get_future_price_volume_data_positional(symbol: str, instrument: str, from_date: str , to_date: str , period: str , currentExpiry: str, nextExpiry: str):
        """
        get contract wise future price volume data set.
        :param instrument:  FUTIDX/FUTSTK
        :param symbol: symbol eg: 'SBIN' / 'BANKNIFTY'
        :param from_date: '17-03-2022' ('dd-mm-YYYY')
        :param to_date: '17-06-2023' ('dd-mm-YYYY')
        :param period: use one {'1D': last day data,
                                '1W': for last 7 days data,
                                '1M': from last month same date,
                                '3M': last 3 month data
                                '6M': last 6 month data}
        :return: pandas.DataFrame
        :raise ValueError if the parameter input is not proper
        """
        validate_date_param(from_date, to_date, period)
        symbol, instrument = cleaning_nse_symbol(symbol=symbol), instrument.upper()
        if instrument not in ['FUTIDX', 'FUTSTK']:
            raise ValueError(f'{instrument} is not a future instrument')

        from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
        nse_df = pd.DataFrame(columns=future_price_volume_data_column_positional)
        from_date = datetime.strptime(from_date, dd_mm_yyyy)
        to_date = datetime.strptime(to_date, dd_mm_yyyy)
        load_days = (to_date - from_date).days
        while load_days > 0:
            if load_days > 90:
                end_date = (from_date + dt.timedelta(90)).strftime(dd_mm_yyyy)
                start_date = from_date.strftime(dd_mm_yyyy)
            else:
                end_date = to_date.strftime(dd_mm_yyyy)
                start_date = from_date.strftime(dd_mm_yyyy)
            data_df = NSE_DATA_CLASS.get_future_price_volume_data_from_nse_positional(symbol=symbol, instrument=instrument,
                                                from_date=start_date, to_date=end_date)
            from_date = from_date + dt.timedelta(91)
            load_days = (to_date - from_date).days
            if nse_df.empty:
                nse_df = data_df
            else:
                nse_df = pd.concat([nse_df, data_df], ignore_index=True)
            
             # Convert data types
            nse_df['TIMESTAMP'] = pd.to_datetime(nse_df['TIMESTAMP'])
            nse_df['EXPIRY_DT'] = pd.to_datetime(nse_df['EXPIRY_DT'])
            numeric_columns = ['OPENING_PRICE', 'TRADE_HIGH_PRICE', 'TRADE_LOW_PRICE', 'CLOSING_PRICE',
                            'LAST_TRADED_PRICE', 'PREV_CLS', 'SETTLE_PRICE', 'TOT_TRADED_QTY',
                            'TOT_TRADED_VAL', 'OPEN_INT', 'CHANGE_IN_OI', 'UNDERLYING_VALUE']
            nse_df[numeric_columns] = nse_df[numeric_columns].apply(pd.to_numeric, errors='coerce')

            expiry_dfs = {}
            for expiry, label in zip([currentExpiry, nextExpiry], ["currExpiry", "nextExpiry"]):
                expiry_df = nse_df[nse_df["EXPIRY_DT"] == expiry]
                expiry_df.set_index('TIMESTAMP', inplace=True)
                expiry_dfs[label] = expiry_df

            res = {
                "futuresData": expiry_dfs
            }
        return res

    @staticmethod
    def get_future_price_volume_data_from_nse_positional(symbol: str, instrument: str, from_date: str, to_date: str):
        origin_url = "https://www.nseindia.com/report-detail/fo_eq_security"
        url = "https://www.nseindia.com/api/historical/foCPV?"
        payload = f"from={from_date}&to={to_date}&instrumentType={instrument}&symbol={symbol}&csv=true"
        try:
            data_dict = nse_urlfetch(url + payload, origin_url=origin_url).json()
        except Exception as e:
            raise ValueError(f" Invalid parameters : NSE error:{e}")
        data_df = pd.DataFrame(data_dict['data']).drop(columns='TIMESTAMP')
        data_df.columns = cleaning_column_name(data_df.columns)
        return data_df[future_price_volume_data_column_positional]

    @staticmethod
    def option_price_volume_data(symbol: str, instrument: str, option_type: str = None, from_date: str = None,
                                to_date: str = None, period: str = None):
        """
        get contract wise option price volume data set. more than 90 days will take more time to collect data.
        :param option_type: PE/CE
        :param instrument:  OPTIDX/OPTSTK
        :param symbol: symbol eg: 'SBIN' / 'BANKNIFTY'
        :param from_date: '17-03-2022' ('dd-mm-YYYY')
        :param to_date: '17-06-2023' ('dd-mm-YYYY')
        :param period: use one {'1D': last day data,
                                '1W': for last 7 days data,
                                '1M': from last month same date,
                                '3M': last 3 month data
                                '6M': last 6 month data}
        :return: pandas.DataFrame
        :raise ValueError if the parameter input is not proper
        """
        validate_date_param(from_date, to_date, period)
        symbol, instrument = cleaning_nse_symbol(symbol=symbol), instrument.upper()
        if instrument not in ['OPTIDX', 'OPTSTK']:
            raise ValueError(f'{instrument} is not a future instrument')

        if option_type and option_type not in ['PE', 'CE']:
            raise ValueError(f'{option_type} is not a valid option type')

        option_type = [option_type] if option_type else ['PE', 'CE']
        from_date, to_date = derive_from_and_to_date(from_date=from_date, to_date=to_date, period=period)
        nse_df = pd.DataFrame(columns=future_price_volume_data_column)
        from_date = datetime.strptime(from_date, dd_mm_yyyy)
        to_date = datetime.strptime(to_date, dd_mm_yyyy)
        load_days = (to_date - from_date).days
        while load_days > 0:
            if load_days > 90:
                end_date = (from_date + dt.timedelta(90)).strftime(dd_mm_yyyy)
                start_date = from_date.strftime(dd_mm_yyyy)
            else:
                end_date = to_date.strftime(dd_mm_yyyy)
                start_date = from_date.strftime(dd_mm_yyyy)
            for opt_typ in option_type:
                data_df = get_option_price_volume_data(symbol=symbol, instrument=instrument, option_type=opt_typ,
                                                    from_date=start_date, to_date=end_date)
                if nse_df.empty:
                    nse_df = data_df
                else:
                    nse_df = pd.concat([nse_df, data_df], ignore_index=True)
            from_date = from_date + dt.timedelta(91)
            load_days = (to_date - from_date).days

        return nse_df

    @staticmethod
    def get_option_price_volume_data(symbol: str, instrument: str, option_type: str, from_date: str, to_date: str):
        origin_url = "https://www.nseindia.com/report-detail/fo_eq_security"
        url = "https://www.nseindia.com/api/historical/foCPV?"
        payload = f"from={from_date}&to={to_date}&instrumentType={instrument}&symbol={symbol}" \
                f"&optionType={option_type}&csv=true"
        try:
            data_dict = nse_urlfetch(url + payload, origin_url=origin_url).json()
        except Exception as e:
            raise ValueError(f" Invalid parameters : NSE error : {e}")
        data_df = pd.DataFrame(data_dict['data']).drop(columns='TIMESTAMP')
        data_df.columns = cleaning_column_name(data_df.columns)
        # print(data_df.columns)
        return data_df[future_price_volume_data_column]

    @staticmethod
    def fno_bhav_copy(trade_date: str):
        """
        new CM-UDiFF Common NSE future option bhav copy from 2018 on wards
        :param trade_date: eg:'20-06-2023'
        :return: pandas Data frame
        """
        trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
        url = 'https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_'
        payload = f"{str(trade_date.strftime('%Y%m%d'))}_F_0000.csv.zip"
        request_bhav = nse_urlfetch(url + payload)
        bhav_df = pd.DataFrame()
        if request_bhav.status_code == 200:
            zip_bhav = zipfile.ZipFile(BytesIO(request_bhav.content), 'r')
            for file_name in zip_bhav.filelist:
                if file_name:
                    bhav_df = pd.read_csv(zip_bhav.open(file_name))
        elif request_bhav.status_code == 403:
            url2 = "https://www.nseindia.com/api/reports?archives=" \
                "%5B%7B%22name%22%3A%22F%26O%20-%20Bhavcopy(csv)%22%2C%22type%22%3A%22archives%22%2C%22category%22" \
                f"%3A%22derivatives%22%2C%22section%22%3A%22equity%22%7D%5D&date={str(trade_date.strftime('%d-%b-%Y'))}" \
                f"&type=equity&mode=single"
            request_bhav = nse_urlfetch(url2)
            if request_bhav.status_code == 200:
                zip_bhav = zipfile.ZipFile(BytesIO(request_bhav.content), 'r')
                for file_name in zip_bhav.filelist:
                    if file_name:
                        bhav_df = pd.read_csv(zip_bhav.open(file_name))
            elif request_bhav.status_code == 403:
                raise FileNotFoundError(f' Data not found, change the date...')
        # bhav_df = bhav_df[['INSTRUMENT', 'SYMBOL', 'EXPIRY_DT', 'STRIKE_PR', 'OPTION_TYP', 'OPEN', 'HIGH', 'LOW',
        #                    'CLOSE', 'SETTLE_PR', 'CONTRACTS', 'VAL_INLAKH', 'OPEN_INT', 'CHG_IN_OI', 'TIMESTAMP']]
        return bhav_df

    @staticmethod
    def participant_wise_open_interest(trade_date: str):
        """
        get FII, DII, Pro, Client wise participant OI data as per traded date
        :param trade_date: eg:'20-06-2023'
        :return: pandas Data frame
        """
        trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
        url = f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{str(trade_date.strftime('%d%m%Y'))}.csv"
        # payload = f"{str(for_date.strftime('%d%m%Y'))}.csv"
        file_chk = nse_urlfetch(url)
        if file_chk.status_code == 404:
            url = f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{str(trade_date.strftime('%d%m%Y'))}.csv"
            file_chk = nse_urlfetch(url)
        if file_chk.status_code != 200:
            raise FileNotFoundError(f" No data available for : {trade_date}")
        try:
            # data_df = pd.read_csv(url, engine='python', sep=',', quotechar='"', on_bad_lines='skip', skiprows=1)
            data_df = pd.read_csv(BytesIO(file_chk.content), on_bad_lines='skip', skiprows=1)
        except:
            data_df = pd.read_csv(BytesIO(file_chk.content), on_bad_lines='skip', skiprows=1)
            data_df.drop(data_df.tail(1).index, inplace=True)
            data_df.columns = [name.replace('\t', '') for name in data_df.columns]
        return data_df

    @staticmethod
    def participant_wise_trading_volume(trade_date: str):
        """
        get FII, DII, Pro, Client wise participant volume data as per traded date
        :param trade_date: eg:'20-06-2023'
        :return: pandas Data frame
        """
        trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
        url = f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_{str(trade_date.strftime('%d%m%Y'))}.csv"
        # payload = f"{str(for_date.strftime('%d%m%Y'))}.csv"
        file_chk = nse_urlfetch(url)
        if file_chk.status_code != 200:
            raise FileNotFoundError(f" No data available for : {trade_date}")
        try:
            data_df = pd.read_csv(BytesIO(file_chk.content), on_bad_lines='skip', skiprows=1)
        except Exception:
            data_df = pd.read_csv(BytesIO(file_chk.content), engine='c', sep=',', quotechar='"',
                                on_bad_lines='skip', skiprows=1)
            data_df.drop(data_df.tail(1).index, inplace=True)
            data_df.columns = [name.replace('\t', '') for name in data_df.columns]
        return data_df

    @staticmethod
    def fii_derivatives_statistics(trade_date: str):
        """
        get FII derivatives statistics as per the traded date provided
        :param trade_date: eg:'20-06-2023'
        :return: pandas dataframe
        """
        t_date = pd.to_datetime(trade_date, format='%d-%m-%Y')
        trade_date = t_date.strftime('%d-%b-%Y')
        url = f"https://nsearchives.nseindia.com/content/fo/fii_stats_{trade_date}.xls"
        file_chk = nse_urlfetch(url)
        if file_chk.status_code != 200:
            raise FileNotFoundError(f" No data available for : {trade_date}")
        try:
            bhav_df = pd.read_excel(BytesIO(file_chk.content), skiprows=3, skipfooter=10).dropna()
            bhav_df.columns = ['fii_derivatives', 'buy_contracts', 'buy_value_in_Cr', 'sell_contracts', 'sell_value_in_Cr',
                            'open_contracts', 'open_contracts_value_in_Cr']
        except Exception as e:
            raise FileNotFoundError(f' FII derivatives statistics not found for : {trade_date} :: NSE error : {e}')
        return bhav_df

    @staticmethod
    def get_nse_option_chain(symbol):
        """
        get NSE option chain for the symbol
        :param symbol: eg:'TCS'/'BANKNIFTY'
        :return: pandas dataframe
        """
        symbol = cleaning_nse_symbol(symbol)
        origin_url = "https://www.nseindia.com/option-chain"
        if any(x in symbol for x in indices_list):
            payload = nse_urlfetch('https://www.nseindia.com/api/option-chain-indices?symbol=' + symbol,
                                origin_url=origin_url)
        else:
            payload = nse_urlfetch('https://www.nseindia.com/api/option-chain-equities?symbol=' + symbol,
                                origin_url=origin_url)
        return payload

    @staticmethod
    def expiry_dates_future():
        """
        get the future and option expiry dates as per stock or index given
        :return: list of dates
        """
        payload = NSE_DATA_CLASS.get_nse_option_chain("TCS").json()
        return payload['records']['expiryDates']

    @staticmethod
    def expiry_dates_option_index():
        """
        get the future and option expiry dates as per stock or index given
        :return: dictionary
        """
        # data_df = pd.DataFrame(columns=['index', 'expiry_date'])
        data_dict = {}
        for ind in indices_list:
            payload = get_nse_option_chain(ind).json()
            data_dict.update({ind: payload['records']['expiryDates']})
        return data_dict

    @staticmethod
    def nse_live_option_chain(symbol: str, expiry_date: str, oi_mode: str = "full"):
        """
        get live nse option chain.
        :param symbol: eg:SBIN/BANKNIFTY
        :param expiry_date: '20-06-2023'
        :param oi_mode: eg: full/compact
        :return: pands dataframe
        """
        payload = get_nse_option_chain(symbol).json()
        if expiry_date:
            exp_date = pd.to_datetime(expiry_date, format='%d-%m-%Y')
            expiry_date = exp_date.strftime('%d-%b-%Y')

        if oi_mode == 'compact':
            col_names = ['Fetch_Time', 'Symbol', 'Expiry_Date', 'CALLS_OI', 'CALLS_Chng_in_OI', 'CALLS_Volume', 'CALLS_IV',
                        'CALLS_LTP', 'CALLS_Net_Chng', 'Strike_Price', 'PUTS_OI', 'PUTS_Chng_in_OI', 'PUTS_Volume',
                        'PUTS_IV', 'PUTS_LTP', 'PUTS_Net_Chng']
        else:
            col_names = ['Fetch_Time', 'Symbol', 'Expiry_Date', 'CALLS_OI', 'CALLS_Chng_in_OI', 'CALLS_Volume', 'CALLS_IV',
                        'CALLS_LTP', 'CALLS_Net_Chng', 'CALLS_Bid_Qty', 'CALLS_Bid_Price', 'CALLS_Ask_Price',
                        'CALLS_Ask_Qty', 'Strike_Price', 'PUTS_Bid_Qty', 'PUTS_Bid_Price', 'PUTS_Ask_Price', 'PUTS_Ask_Qty',
                        'PUTS_Net_Chng', 'PUTS_LTP', 'PUTS_IV', 'PUTS_Volume', 'PUTS_Chng_in_OI', 'PUTS_OI']

        oi_data = pd.DataFrame(columns=col_names)

        oi_row = {'Fetch_Time': None, 'Symbol': None, 'Expiry_Date': None, 'CALLS_OI': 0, 'CALLS_Chng_in_OI': 0, 'CALLS_Volume': 0,
                'CALLS_IV': 0, 'CALLS_LTP': 0, 'CALLS_Net_Chng': 0, 'CALLS_Bid_Qty': 0, 'CALLS_Bid_Price': 0,
                'CALLS_Ask_Price': 0, 'CALLS_Ask_Qty': 0, 'Strike_Price': 0, 'PUTS_OI': 0, 'PUTS_Chng_in_OI': 0,
                'PUTS_Volume': 0, 'PUTS_IV': 0, 'PUTS_LTP': 0, 'PUTS_Net_Chng': 0, 'PUTS_Bid_Qty': 0,
                'PUTS_Bid_Price': 0, 'PUTS_Ask_Price': 0, 'PUTS_Ask_Qty': 0}

        # print(expiry_date)
        for m in range(len(payload['records']['data'])):
            if not expiry_date or (payload['records']['data'][m]['expiryDate'] == expiry_date):
                try:
                    oi_row['Expiry_Date'] = payload['records']['data'][m]['expiryDate']
                    oi_row['CALLS_OI'] = payload['records']['data'][m]['CE']['openInterest']
                    oi_row['CALLS_Chng_in_OI'] = payload['records']['data'][m]['CE']['changeinOpenInterest']
                    oi_row['CALLS_Volume'] = payload['records']['data'][m]['CE']['totalTradedVolume']
                    oi_row['CALLS_IV'] = payload['records']['data'][m]['CE']['impliedVolatility']
                    oi_row['CALLS_LTP'] = payload['records']['data'][m]['CE']['lastPrice']
                    oi_row['CALLS_Net_Chng'] = payload['records']['data'][m]['CE']['change']
                    if oi_mode == 'full':
                        oi_row['CALLS_Bid_Qty'] = payload['records']['data'][m]['CE']['bidQty']
                        oi_row['CALLS_Bid_Price'] = payload['records']['data'][m]['CE']['bidprice']
                        oi_row['CALLS_Ask_Price'] = payload['records']['data'][m]['CE']['askPrice']
                        oi_row['CALLS_Ask_Qty'] = payload['records']['data'][m]['CE']['askQty']
                except KeyError:
                    oi_row['CALLS_OI'], oi_row['CALLS_Chng_in_OI'], oi_row['CALLS_Volume'], oi_row['CALLS_IV'], oi_row[
                        'CALLS_LTP'], oi_row['CALLS_Net_Chng'] = 0, 0, 0, 0, 0, 0
                    if oi_mode == 'full':
                        oi_row['CALLS_Bid_Qty'], oi_row['CALLS_Bid_Price'], oi_row['CALLS_Ask_Price'], oi_row[
                            'CALLS_Ask_Qty'] = 0, 0, 0, 0
                    pass

                oi_row['Strike_Price'] = payload['records']['data'][m]['strikePrice']

                try:
                    oi_row['PUTS_OI'] = payload['records']['data'][m]['PE']['openInterest']
                    oi_row['PUTS_Chng_in_OI'] = payload['records']['data'][m]['PE']['changeinOpenInterest']
                    oi_row['PUTS_Volume'] = payload['records']['data'][m]['PE']['totalTradedVolume']
                    oi_row['PUTS_IV'] = payload['records']['data'][m]['PE']['impliedVolatility']
                    oi_row['PUTS_LTP'] = payload['records']['data'][m]['PE']['lastPrice']
                    oi_row['PUTS_Net_Chng'] = payload['records']['data'][m]['PE']['change']
                    if oi_mode == 'full':
                        oi_row['PUTS_Bid_Qty'] = payload['records']['data'][m]['PE']['bidQty']
                        oi_row['PUTS_Bid_Price'] = payload['records']['data'][m]['PE']['bidprice']
                        oi_row['PUTS_Ask_Price'] = payload['records']['data'][m]['PE']['askPrice']
                        oi_row['PUTS_Ask_Qty'] = payload['records']['data'][m]['PE']['askQty']
                except KeyError:
                    oi_row['PUTS_OI'], oi_row['PUTS_Chng_in_OI'], oi_row['PUTS_Volume'], oi_row['PUTS_IV'], oi_row[
                        'PUTS_LTP'], oi_row['PUTS_Net_Chng'] = 0, 0, 0, 0, 0, 0
                    if oi_mode == 'full':
                        oi_row['PUTS_Bid_Qty'], oi_row['PUTS_Bid_Price'], oi_row['PUTS_Ask_Price'], oi_row[
                            'PUTS_Ask_Qty'] = 0, 0, 0, 0

                # if oi_mode == 'full':
                #     oi_row['CALLS_Chart'], oi_row['PUTS_Chart'] = 0, 0
                if oi_data.empty:
                    oi_data = pd.DataFrame([oi_row]).copy()
                else:
                    oi_data = pd.concat([oi_data, pd.DataFrame([oi_row])], ignore_index=True)
                oi_data['Symbol'] = symbol
                oi_data['Fetch_Time'] = payload['records']['timestamp']
        return oi_data

    @staticmethod
    def fno_security_in_ban_period(trade_date: str):
        """
        To get the list of securities which are baned from fno segment to trade
        :param trade_date: eg:'20-06-2023'
        :return: pandas Data frame
        """
        trade_date = datetime.strptime(trade_date, dd_mm_yyyy)
        url = 'https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_'
        payload = f"{str(trade_date.strftime('%d%m%Y'))}.csv"
        request = nse_urlfetch(url + payload)
        securities = []
        if request.status_code == 200:
            lines = request.content.decode("utf-8").strip().split("\n")
            securities = [line.split(",")[1] for line in lines[1:]]
        elif request.status_code == 403:
            url2 = "https://www.nseindia.com/api/reports?archives=" \
                "%5B%7B%22name%22%3A%22F%26O%20-%20Security%20in%20ban%20period%22%2C%22type%22%3A%22archives%22%2C%22category%22" \
                f"%3A%22derivatives%22%2C%22section%22%3A%22equity%22%7D%5D&date={str(trade_date.strftime('%d-%b-%Y'))}" \
                f"&type=equity&mode=single"
            request = nse_urlfetch(url2)
            if request.status_code == 200:
                lines = request.content.decode("utf-8").strip().split("\n")
                securities = [line.split(",")[1] for line in lines[1:]]
            elif request.status_code == 403:
                raise FileNotFoundError(f' Data not found, change the date...')
        return securities


# if __name__ == '__main__':
    # df = future_price_volume_data("BANKNIFTY", "FUTIDX", from_date='17-06-2023', to_date='19-06-2023', period='1W')
    # df = option_price_volume_data('NIFTY', 'OPTIDX', period='1D')
    # df = get_nse_option_chain(symbol='TCS')
    # df = fii_derivatives_statistics(trade_date='16-09-2024')
    # df = fno_security_in_ban_period(trade_date='26-03-2025')
    # df = expiry_dates_option_index()
    # df = fno_bhav_copy('17-02-2025')
    # print(df)
    # print(df.columns)
    # print(df[df['EXPIRY_DT']=='27-Jul-2023'])
