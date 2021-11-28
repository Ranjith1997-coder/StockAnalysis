import plotly.graph_objects as go
from optionOpstraCollection import getIVChartData
from plotly.subplots import make_subplots
from datetime import datetime

def getEventStartIndex(historyStartDate , event_date_series):

    historyStartDate_datetime = datetime.strptime(historyStartDate,"%Y-%m-%d")
    index = 0
    for date in event_date_series:
        date_dateTime = datetime.strptime(date,"%Y-%m-%d")
        if date_dateTime > historyStartDate_datetime:
            return index
        index += 1
    return -1

if __name__ =='__main__':
    ticker = input("Enter the stock for IV chart: ")
    # months = input("Enter the number of months :")
    # ticker = "LALPATHLAB"
    months = 3
    event_index = -1
    No_of_dataPoints = months*30


    (df_events, df_data) = getIVChartData(ticker)
    df_data = df_data.tail(No_of_dataPoints)
    if not df_events.empty:
        event_index = getEventStartIndex(df_data['Date'].iloc[0], df_events['Date'])

    # print(df_data)

    fig = make_subplots(rows=3, cols=1)

    fig.add_trace(go.Candlestick(x=df_data['Date'],
                    open=df_data['Open'],
                    high=df_data['High'],
                    low=df_data['Low'],
                    close=df_data['Close'],name=ticker+" Price"),row=1,col=1)

    if (event_index != -1):
        highDataForEventDisplay = df_data.loc[
                                      df_data['Date'].isin(tuple(df_events['Date'].iloc[event_index:])), 'High'] + 10
        fig.add_trace(go.Scatter(x= df_events['Date'].iloc[event_index:], y= highDataForEventDisplay,name="Events",mode='markers', marker=dict(
            color='black',
            size=10,
        )),row=1,col=1 )

    fig.add_trace(go.Scatter(x=df_data['Date'], y=df_data['ImpVol'],name="Implied Volatility"),row=2,col=1)
    fig.add_trace(go.Scatter(x=df_data['Date'], y=df_data['HV30'], name="HV30"), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_data['Date'], y=df_data['HV10'], name="HV10"), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_data['Date'], y=df_data['IVP'], name="IV Percentile"), row=3, col=1)

    fig.update_layout(xaxis_rangeslider_visible=False, title = ticker, hovermode='x unified')



    fig.show()

