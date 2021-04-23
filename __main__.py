import yfinance as yf
import constants as c


class Stock:
    def __init__(self):
        pass


    @classmethod
    def createStockObj(cls, code):
        try:
            result = yf.download(code, period="2y", rounding=True)
        except ConnectionError:
            raise ConnectionError
        except Exception as e:
            raise e

        obj = cls()
        obj.code = code
        obj.info = result

        return obj


if __name__ == '__main__':

    stocksList = []
    #
    # for stock in c.stocks:
    #     try:
    #         stocksList.append(Stock.createStockObj(stock))
    #     except Exception:
    #         break

    #sensex = Stock.createStockObj(c.stocks[0])

    result = yf.download(c.stocks[0], period="2y", rounding=True)
    print(type(result))
















