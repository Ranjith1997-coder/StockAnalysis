#!/bin/bash
cd ~/StockAnalysis/
source env/bin/activate
export PYTHONPATH=$PYTHONPATH:~/StockAnalysis/
python intraday/intraday_monitor.py