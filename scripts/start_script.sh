#!/bin/bash
cd ~/StockAnalysis/
source env/bin/activate
export PRODUCTION="True"
export SHUTDOWN="True"
python intraday/intraday_monitor.py