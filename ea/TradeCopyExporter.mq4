//+------------------------------------------------------------------+
//|                                        TradeCopyExporter.mq4      |
//|  Minimal trade-event exporter for the mt4-trade-copier-bridge.   |
//|                                                                  |
//|  Purpose: watch this (demo) account's open trades and append a   |
//|  line to MQL4/Files/trade_events.csv every time a position opens |
//|  or closes. A separate Python bridge tails that file and         |
//|  translates each event into an Alpaca paper order.               |
//|                                                                  |
//|  Why a custom EA instead of vobornik/mt4-trade-copy: that EA is  |
//|  built to copy MT4 -> MT4 (master/slave via files/GlobalVars).   |
//|  We don't need copying inside MetaTrader at all -- we only need  |
//|  a reliable event feed OUT of the terminal, which is a much      |
//|  smaller, more auditable job. Same append-only-file transport    |
//|  vobornik uses, just the export half.                            |
//|                                                                  |
//|  Wire format (must stay byte-identical to src/mt4_demo_emitter): |
//|    event_id,ts_utc,action,ticket,symbol,order_type,lots,         |
//|    open_price,close_price,sl,tp                                  |
//|  - header line written once when the file is first created       |
//|  - action    : OPEN | CLOSE                                      |
//|  - order_type: BUY | SELL  (pending orders are ignored)          |
//|  - ts_utc    : broker server time, yyyy.MM.dd HH:mm:ss           |
//|  - prices    : forex prices as-is; the bridge does NOT use the   |
//|                open_price for equity sizing (see symbol_map.py)   |
//+------------------------------------------------------------------+
#property strict

input string EventFileName = "trade_events.csv";  // written under MQL4/Files/

// Tickets we've already emitted an OPEN for and not yet a CLOSE.
// Kept in memory; on restart we re-baseline (see OnInit) so we never
// double-emit an OPEN for a position that was already open.
long    knownTickets[];
int     eventCounter = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   // Baseline: record every currently-open ticket WITHOUT emitting an
   // OPEN event. These positions predate the exporter; emitting them
   // would tell the bridge to copy trades that are already live.
   ArrayResize(knownTickets, 0);
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderType() != OP_BUY && OrderType() != OP_SELL) continue;  // skip pendings
      AddTicket(OrderTicket());
   }
   // Make sure the header exists so the bridge can parse column names.
   EnsureHeader();
   Print("TradeCopyExporter initialised; baseline open tickets: ", ArraySize(knownTickets));
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason) { }

//+------------------------------------------------------------------+
void OnTick()
{
   // 1) Detect newly-opened positions -> emit OPEN.
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderType() != OP_BUY && OrderType() != OP_SELL) continue;
      long tk = OrderTicket();
      if(!IsKnown(tk))
      {
         WriteEvent("OPEN", tk, OrderSymbol(), OrderType(), OrderLots(),
                    OrderOpenPrice(), 0.0, OrderStopLoss(), OrderTakeProfit(),
                    OrderOpenTime());
         AddTicket(tk);
      }
   }

   // 2) Detect positions that vanished from the live pool -> emit CLOSE.
   //    We copy knownTickets, then for each one check it's still live;
   //    if not, pull its close details from history.
   int n = ArraySize(knownTickets);
   for(int j = n - 1; j >= 0; j--)
   {
      long tk = knownTickets[j];
      if(IsStillOpen(tk)) continue;

      if(OrderSelect((int)tk, SELECT_BY_TICKET, MODE_HISTORY))
      {
         WriteEvent("CLOSE", tk, OrderSymbol(), OrderType(), OrderLots(),
                    OrderOpenPrice(), OrderClosePrice(), OrderStopLoss(),
                    OrderTakeProfit(), OrderCloseTime());
      }
      else
      {
         // Couldn't resolve from history; still emit a CLOSE so the
         // bridge can flatten its copy, with prices left at 0.
         WriteEvent("CLOSE", tk, "UNKNOWN", OP_BUY, 0.0, 0.0, 0.0, 0.0, 0.0,
                    TimeCurrent());
      }
      RemoveTicketAt(j);
   }
}

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
bool IsStillOpen(long ticket)
{
   for(int i = 0; i < OrdersTotal(); i++)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderTicket() == ticket) return true;
   }
   return false;
}

bool IsKnown(long ticket)
{
   for(int i = 0; i < ArraySize(knownTickets); i++)
      if(knownTickets[i] == ticket) return true;
   return false;
}

void AddTicket(long ticket)
{
   int n = ArraySize(knownTickets);
   ArrayResize(knownTickets, n + 1);
   knownTickets[n] = ticket;
}

void RemoveTicketAt(int idx)
{
   int n = ArraySize(knownTickets);
   for(int i = idx; i < n - 1; i++) knownTickets[i] = knownTickets[i + 1];
   ArrayResize(knownTickets, n - 1);
}

string OrderTypeStr(int t) { return (t == OP_BUY) ? "BUY" : "SELL"; }

void EnsureHeader()
{
   // FILE_READ first: if the file already has content, don't rewrite it.
   int fh = FileOpen(EventFileName, FILE_READ | FILE_CSV | FILE_ANSI);
   if(fh != INVALID_HANDLE)
   {
      bool empty = FileIsEnding(fh);
      FileClose(fh);
      if(!empty) return;  // header already present
   }
   int wh = FileOpen(EventFileName, FILE_WRITE | FILE_CSV | FILE_ANSI);
   if(wh == INVALID_HANDLE) { Print("ERROR: cannot open ", EventFileName); return; }
   FileWrite(wh, "event_id", "ts_utc", "action", "ticket", "symbol",
             "order_type", "lots", "open_price", "close_price", "sl", "tp");
   FileClose(wh);
}

void WriteEvent(string action, long ticket, string symbol, int otype,
                double lots, double openPrice, double closePrice,
                double sl, double tp, datetime ts)
{
   // Append mode: open for read/write, seek to end, write one line.
   int fh = FileOpen(EventFileName, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI);
   if(fh == INVALID_HANDLE) { Print("ERROR: cannot append to ", EventFileName); return; }
   FileSeek(fh, 0, SEEK_END);
   eventCounter++;
   FileWrite(fh, eventCounter,
             TimeToString(ts, TIME_DATE | TIME_SECONDS),
             action, ticket, symbol, OrderTypeStr(otype),
             DoubleToString(lots, 2),
             DoubleToString(openPrice, 5),
             DoubleToString(closePrice, 5),
             DoubleToString(sl, 5),
             DoubleToString(tp, 5));
   FileClose(fh);
   Print("exported ", action, " ticket=", ticket, " ", symbol, " ", OrderTypeStr(otype));
}
//+------------------------------------------------------------------+
