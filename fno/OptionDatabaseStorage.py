import mysql.connector


class OptionDatabase:

    def __init__(self,username,password,host,database):
        self.user = username
        self.password = password
        self.host = host
        self.database = database
        self.cnx = None
        self.stockTableName = "stock_table"
        self.optionsDataTableName = "option_data"

    def connectToDatabase(self):

        self.cnx = mysql.connector.connect(user=self.user, password=self.password,
                                      host=self.host,
                                      database=self.database)



    def closeConnection(self):
        if (self.cnx is not None) and (self.cnx.is_connected()):
            self.cnx.close()


    def addNewCompany(self,companyName, stockSymbol):
        try:
            self.connectToDatabase()
        except Exception as e:
            print("connection failed {}".format(e.__doc__) )
            return

        addCompany = ("INSERT INTO {} (company_name, stock_symbol) VALUES (%s, %s)").format(self.stockTableName)

        parm = list()

        for cName, sSymbol in zip(companyName, stockSymbol):
            parm.append((cName,sSymbol))

        cursor = self.cnx.cursor()

        cursor.executemany(addCompany,parm)

        self.cnx.commit()
        cursor.close()

        self.closeConnection()

    def addOptionData(self,companyName):

        if (not companyName):
            raise Exception("invalid paramerets")

        try:
            self.connectToDatabase()
        except Exception as e:
            print("connection failed {}".format(e.__doc__) )
            return

        GetSerialNoQuery = ("SELECT s_no FROM {} WHERE stock_symbol = '{}';").format(self.stockTableName, companyName)

        cursor = self.cnx.cursor()

        cursor.execute(GetSerialNoQuery)

        cursor.s

