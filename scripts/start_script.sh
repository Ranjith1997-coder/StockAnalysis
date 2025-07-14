#!/bin/bash
cd ~/StockAnalysis/
source env/bin/activate
export PRODUCTION="True"
python intraday/intraday_monitor.py