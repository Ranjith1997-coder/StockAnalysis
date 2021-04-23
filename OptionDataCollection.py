import requests
from datetime import date
from os import mkdir
from constants import indexSymbolForNSE,  stocksSymbolForNSE


stockURL = "https://www.nseindia.com/api/option-chain-equities"
indexURL = "https://www.nseindia.com/api/option-chain-indices"
cookie = dict(Cookie='_ga=GA1.2.888455025.1592897115; ext_name=ojplmecpdpgccookcobabopnaifgidhf; nsit=0dX0tisCgi0bfBg-bAGFAQ7k; nseappid=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJhcGkubnNlIiwiYXVkIjoiYXBpLm5zZSIsImlhdCI6MTYxNTkxNjI0NCwiZXhwIjoxNjE1OTE5ODQ0fQ.mBU-PwxxP3IP_UrnZQI9lOJZ6Uo95MtAy7xESTJ60x4; AKA_A2=A; bm_mi=C73CA33D9AC3E773F077F1F131407833~0EEYE9eSNFWkBkVn5n86atDXhuZHxW88j9YUKFU1QajK+FPZ/zXwlnXO3LyLLbakxVb02Kg74+QmLsZVcZNUDHo2c4OjMdE3/P1f7+YvrnLrWHhaEJJLI4gWakHnOhQA7qA4ONX6IIVwINpAJVMilSr8fdsYonbllUcZL6WCvMbVZ5NPmnQL3OBJdE8UhIBoXqqr+CF/quI0RoQhQOAY8d1HuZwQM6DXHZkvcHz/6x0RmPsODZXGwKllvoiVqkJ7PTjD3852BLrJNL3sBeqRm+SdR9ywvm2yyXv8akNoZIg=; ak_bmsc=CAD2C5BD027271836C364BE886547E81173E650477270000CEEC5060C0D22E0A~plGMxOArkq+vsxZ0xkeyPnfqWalCHA3Vw/jdyMGGvhAUr+7gVGIAR3d05Ey6MoNhdqHK03KBqJ+nfWvDT7L7pskVba2Sy0OQGkpOEEELmkMSttjHgOJLrcLq9tSyIiWfpEZN98+ej9pHl9EU76Isw7C4Vj2tT446j4lgX2P3zbdL/MmVJ/hXoyZMLAw4Wdli50tjpNMptYRK/9wpQgnKFGaYlTwtvrGoem4k/LnGTjSEPVZinWAEsRGhi2xm0ihL/8; _gid=GA1.2.1467863461.1615916247; _gat_UA-143761337-1=1; RT="sl=1&ss=km62uzo0&tt=1x9&z=1&dm=nseindia.com&si=8f8f8505-4f1b-4caf-80f9-1d79625848e3&bcn=%2F%2F684fc53c.akstat.io%2F&ld=67y91q"; bm_sv=DAF008FA97D3BBE179180F347CFCB2BE~82C981Gfk/hupo34fdpbymWLBq23aLmQKlSFwhXviE4ncGwQ8yUTxfVtQiQKhlY7zUOs0Fo406ktX4/RGkewg2IF/AeV4E5Pp9XTvaCMwA3WfZjwdKyJ/RXjeOyE1n1oukg+kp7oAQFA49XeFMLIVGxPg1TnePihEll383Bl3IE=')
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36'}
dirName = "./OptionsData/" + str(date.today())



try:
    mkdir(dirName)
except FileNotFoundError as e:
    print("Path not available "+ e.filename)
except FileExistsError:
    pass
except Exception as e:
    print("Exception occured during new Directory creation"+ str(e.__class__))


for symbol in indexSymbolForNSE:
    print(symbol)
    res = requests.get(indexURL,params=symbol , cookies = cookie, headers= headers)
    print(res)
    if (res.status_code == 200):
        with open(dirName+'/'+symbol['symbol']+'.json', 'w') as fd:
            fd.write(res.text)



for symbol in stocksSymbolForNSE:
    print(symbol)
    res = requests.get(stockURL,params=symbol , cookies = cookie, headers= headers)
    print(res)
    if (res.status_code == 200):
        with open(dirName+'/'+symbol['symbol']+'.json', 'w') as fd:
            fd.write(res.text)

