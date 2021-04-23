from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from time import sleep

ZERODHA_URL = "https://kite.zerodha.com/"
USERNAME = "QR5450"
PASSWORD = "Tgranjith@1"
PASSCODE = "191812"
MAX_NUM_OF_CHART = 1

CHART_URL = "https://kite.zerodha.com/chart/ext/tvc/NSE/COALINDIA/5215745"

options = Options();
options.add_argument("--window-size=1920,1080");

wd = webdriver.Chrome(options= options)
wd.get(ZERODHA_URL)
assert "Kite" in wd.title


cookie = {'name' : 'WZRK_G' ,'value' : 'ade3a1e280d148b1a9647215edd9aed0'}
wd.add_cookie(cookie)
cookie = {'name' :'_ga' ,'value' : 'GA1.2.284240585.1588767120'}
wd.add_cookie(cookie)
cookie = {'name' :'ext_name' ,'value' : 'ojplmecpdpgccookcobabopnaifgidhf'}
wd.add_cookie(cookie)
cookie = {'name' :'_gid' ,'value' : 'GA1.2.428081058.1607529127'}
wd.add_cookie(cookie)

wd.implicitly_wait(5)


userID  = wd.find_element_by_id("userid")
userID.send_keys(USERNAME)

passwordText  = wd.find_element_by_id("password")
passwordText.send_keys(PASSWORD,Keys.RETURN)

passcodeText = wd.find_element_by_id("pin")
passcodeText.send_keys(PASSCODE,Keys.RETURN)


for i in range(MAX_NUM_OF_CHART):
    wd.execute_script("window.open('');")
    wd.switch_to.window(wd.window_handles[i+1])
    wd.get(CHART_URL)
    print(wd.get_cookies())

print(wd.window_handles)


# sleep(5)
# body = wd.find_element_by_tag_name("body")
# print(body)
#
# body.send_keys(Keys.CONTROL + 't')







