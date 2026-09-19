#!/usr/bin/env python3
import serial, subprocess, time
from collections import deque
from pna_ui_common import *

SERIAL_PORT="/dev/serial0"
SERIAL_BAUD=115200
PAGES=["LIVE SECURITY","AP MONITOR","RF WATCH","BLE MONITOR","EVENT LOG"]

class BruceSafe:
    def __init__(self):
        self.display=ST7796S()
        self.enc=EncoderPair(4)
        self.ser=serial.Serial(SERIAL_PORT,SERIAL_BAUD,timeout=0.02)
        self.page=0; self.selection=0; self.last_esp=0.0
        self.wifi={}; self.rf={}; self.ble={}; self.events=deque(maxlen=24)
        self.last_draw=0.0

    def close(self):
        try:self.ser.close()
        except Exception:pass
        try:self.enc.close()
        except Exception:pass
        try:self.display.close()
        except Exception:pass

    def process(self,line):
        now=time.time()
        if line=="ESP32_ALIVE":
            self.last_esp=now; return
        if line.startswith("WIFI|"):
            p=line.split("|")
            if len(p)>=6:
                try:self.wifi[p[2].lower()]={"ssid":p[1],"bssid":p[2],"rssi":int(p[3]),"channel":int(p[4]),"security":p[5],"last":now}
                except ValueError:pass
            return
        if line.startswith("RF|"):
            p=line.split("|")
            if len(p)>=10:
                try:
                    ch=int(p[1])
                    self.rf[ch]={"channel":ch,"packets":int(p[2]),"bytes":int(p[3]),"avg":int(p[4]),"peak":int(p[5]),"activity":int(p[6]),"mgmt":int(p[7]),"data":int(p[8]),"ctrl":int(p[9]),"last":now}
                except ValueError:pass
            return
        if line.startswith("BLE|"):
            p=line.split("|")
            if len(p)>=4:
                try:self.ble[p[2].lower()]={"name":p[1],"address":p[2],"rssi":int(p[3]),"last":now}
                except ValueError:pass
            return
        if line.startswith("SEC|"):
            p=line.split("|",4)
            self.events.appendleft({"time":now,"type":p[1] if len(p)>1 else "UNKNOWN","channel":p[2] if len(p)>2 else "-","count":p[3] if len(p)>3 else "1","detail":p[4] if len(p)>4 else ""})

    def poll_uart(self):
        for _ in range(80):
            if self.ser.in_waiting<=0: break
            line=self.ser.readline().decode("utf-8",errors="replace").strip()
            if line:self.process(line)

    def sorted_wifi(self):
        return sorted(self.wifi.values(),key=lambda x:x["rssi"],reverse=True)

    def threat(self):
        recent=[e for e in self.events if time.time()-e["time"]<=20]
        if len(recent)>=5:return "HIGH",RED
        if recent:return "WATCH",YELLOW
        return "LOW",GREEN

    def draw_live(self,draw):
        level,color=self.threat()
        panel(draw,(18,54,462,118))
        text(draw,(30,68),"THREAT LEVEL",FONT_SM,MUTED)
        text(draw,(30,91),level,FONT_LG,color)
        stats=[("Wi-Fi APs",len(self.wifi)),("BLE devices",len(self.ble)),("RF channels",len(self.rf)),("Events",len(self.events))]
        x=22
        for label,value in stats:
            panel(draw,(x,132,x+102,190),PANEL2)
            text(draw,(x+10,144),label,FONT_SM,MUTED)
            text(draw,(x+10,164),value,FONT_MD,TEXT)
            x+=110
        if self.rf:
            hot=max(self.rf.values(),key=lambda i:(i["activity"],i["packets"]))
            panel(draw,(18,204,462,282))
            text(draw,(30,216),"CHANNEL WATCH",FONT_TITLE,PURPLE)
            text(draw,(30,244),f"Hot channel: {hot['channel']}   Activity: {hot['activity']}%   Peak: {hot['peak']} dBm",FONT_BODY,TEXT)
            text(draw,(30,267),f"Mgmt {hot['mgmt']}   Data {hot['data']}   Ctrl {hot['ctrl']}",FONT_SM,MUTED)

    def draw_ap(self,draw):
        aps=self.sorted_wifi()
        if not aps:
            text(draw,(28,95),"Waiting for Wi-Fi scan...",FONT_MD,MUTED); return
        self.selection=max(0,min(self.selection,len(aps)-1))
        rows=7
        start=max(0,min(self.selection-rows//2,max(0,len(aps)-rows)))
        y=58
        for i in range(start,min(start+rows,len(aps))):
            ap=aps[i]; sel=i==self.selection
            panel(draw,(16,y,464,y+30),PANEL2 if sel else PANEL,GREEN if sel else None,6)
            text(draw,(25,y+15),ap["ssid"][:25],FONT_SM,TEXT,anchor="lm")
            text(draw,(285,y+15),f"CH{ap['channel']}",FONT_SM,MUTED,anchor="lm")
            text(draw,(334,y+15),ap["security"][:9],FONT_SM,MUTED,anchor="lm")
            text(draw,(414,y+15),f"{ap['rssi']} dBm",FONT_SM,GREEN,anchor="lm")
            y+=33

    def draw_rf(self,draw):
        if not self.rf:
            text(draw,(28,95),"Waiting for RF sweep...",FONT_MD,MUTED); return
        y=64
        for ch in range(1,12):
            item=self.rf.get(ch)
            text(draw,(22,y),f"CH {ch:>2}",FONT_SM,TEXT)
            if item:
                a=max(0,min(100,item["activity"]))
                draw.rounded_rectangle((78,y+1,350,y+14),radius=4,fill=PANEL2)
                w=int(272*a/100)
                if w:draw.rounded_rectangle((78,y+1,78+w,y+14),radius=4,fill=PURPLE)
                text(draw,(364,y),f"{a:>3}%",FONT_SM,TEXT)
                text(draw,(414,y),f"{item['peak']:>4}",FONT_SM,MUTED)
            y+=19

    def draw_ble(self,draw):
        ds=sorted(self.ble.values(),key=lambda x:x["rssi"],reverse=True)
        if not ds:
            text(draw,(28,95),"Waiting for BLE advertisements...",FONT_MD,MUTED); return
        self.selection=max(0,min(self.selection,len(ds)-1))
        y=62
        for i,d in enumerate(ds[:7]):
            sel=i==self.selection
            panel(draw,(16,y,464,y+30),PANEL2 if sel else PANEL,GREEN if sel else None,6)
            text(draw,(25,y+15),d["name"][:24],FONT_SM,TEXT,anchor="lm")
            text(draw,(306,y+15),d["address"][-8:],FONT_SM,MUTED,anchor="lm")
            text(draw,(414,y+15),f"{d['rssi']} dBm",FONT_SM,GREEN,anchor="lm")
            y+=33

    def draw_events(self,draw):
        if not self.events:
            text(draw,(28,83),"No security events yet.",FONT_MD,GREEN)
            text(draw,(28,112),"Future ESP SEC|... events will appear here.",FONT_SM,MUTED)
            return
        y=60
        for e in list(self.events)[:8]:
            age=int(time.time()-e["time"])
            text(draw,(20,y),f"{e['type']:<15} CH {e['channel']:<3} x{e['count']:<4} {age:>2}s ago",FONT_SM,YELLOW)
            y+=27

    def draw(self):
        image,draw=new_frame()
        page=PAGES[self.page]
        header(draw,"BRUCE SAFE","ESP ON" if time.time()-self.last_esp<=10 else "ESP WAIT")
        text(draw,(18,46),page,FONT_TITLE,GREEN)
        if page=="LIVE SECURITY":self.draw_live(draw)
        elif page=="AP MONITOR":self.draw_ap(draw)
        elif page=="RF WATCH":self.draw_rf(draw)
        elif page=="BLE MONITOR":self.draw_ble(draw)
        elif page=="EVENT LOG":self.draw_events(draw)
        footer(draw,"L page   R select   Hold R 3s: selector   Hold L 5s: power")
        self.display.show(image)

    def handle(self,e):
        page=PAGES[self.page]
        if e=="LEFT_CW":self.page=(self.page+1)%len(PAGES);self.selection=0
        elif e=="LEFT_CCW":self.page=(self.page-1)%len(PAGES);self.selection=0
        elif e=="RIGHT_CW" and page in ("AP MONITOR","BLE MONITOR"):self.selection+=1
        elif e=="RIGHT_CCW" and page in ("AP MONITOR","BLE MONITOR"):self.selection=max(0,self.selection-1)
        elif e=="RIGHT_HOLD":raise SystemExit(0)
        elif e=="LEFT_HOLD":
            self.close()
            subprocess.run(["/usr/bin/sudo","/usr/bin/systemctl","poweroff"],check=False)
            raise SystemExit(0)

    def run(self):
        try:
            while True:
                self.poll_uart()
                for e in self.enc.poll():self.handle(e)
                now=time.monotonic()
                if now-self.last_draw>=0.30:
                    self.draw();self.last_draw=now
                time.sleep(0.002)
        finally:self.close()

if __name__=="__main__":
    BruceSafe().run()
