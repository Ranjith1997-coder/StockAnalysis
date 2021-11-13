import plotly.graph_objects as go
from optionOpstraCollection import getIVChartData
import pandas as pd
from plotly.subplots import make_subplots

if __name__ =='__main__':
    ticker = input("Enter the stock for IV chart: ")
    # months = input("Enter the number of months :")
    # ticker = "LALPATHLAB"
    months = 3
    No_of_dataPoints = months*30


    (events, data) = getIVChartData(ticker)
    df_events = pd.DataFrame(data=events)
    df_data = pd.DataFrame(data=data)
    df_data = df_data.tail(No_of_dataPoints)

    print(df_data)

    fig = make_subplots(rows=3, cols=1)

    fig.add_trace(go.Candlestick(x=df_data['Date'],
                    open=df_data['Open'],
                    high=df_data['High'],
                    low=df_data['Low'],
                    close=df_data['Close'],name=ticker+" Price"),row=1,col=1)

    fig.add_trace(go.Scatter(x=df_data['Date'], y=df_data['ImpVol'],name="Implied Volatility"),row=2,col=1)
    fig.add_trace(go.Scatter(x=df_data['Date'], y=df_data['HV30'], name="HV30"), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_data['Date'], y=df_data['HV10'], name="HV10"), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_data['Date'], y=df_data['IVP'], name="IV Percentile"), row=3, col=1)



    fig.update_layout(xaxis_rangeslider_visible=False, title = ticker, hovermode='x unified')



    fig.show()

