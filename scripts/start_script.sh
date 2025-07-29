#!/bin/bash
cd ~/StockAnalysis/
source env/bin/activate
export PRODUCTION="1"
export SHUTDOWN="1"
export ENABLE_DERIVATIVES="0"
python intraday/intraday_monitor.py