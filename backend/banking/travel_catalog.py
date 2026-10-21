"""Deterministic mock inventory. No real carriers, properties, or availability."""
from datetime import datetime, timedelta
from hashlib import sha256
from fastapi import HTTPException
from .db import TZ

CITIES = {
    '北京':['朝阳区','东城区','海淀区'], '上海':['浦东新区','黄浦区','静安区'],
    '杭州':['西湖区','上城区','滨江区'], '广州':['天河区','越秀区','海珠区'],
    '深圳':['南山区','福田区','罗湖区'], '成都':['锦江区','武侯区','青羊区'],
    '重庆':['渝中区','江北区','南岸区'], '西安':['雁塔区','碑林区','新城区'],
    '南京':['玄武区','秦淮区','鼓楼区'], '苏州':['姑苏区','工业园区','吴中区'],
    '武汉':['武昌区','江汉区','洪山区'], '长沙':['岳麓区','芙蓉区','天心区'],
    '厦门':['思明区','湖里区','集美区'], '青岛':['市南区','崂山区','市北区'],
    '天津':['和平区','河西区','南开区'], '昆明':['五华区','盘龙区','官渡区'],
    '三亚':['吉阳区','天涯区','海棠区'], '郑州':['金水区','二七区','中原区'],
    '济南':['历下区','市中区','槐荫区'], '合肥':['蜀山区','包河区','庐阳区'],
}

def clock():return datetime.now(TZ)

def city(value):
    value=value.removesuffix('市')
    if value not in CITIES:raise HTTPException(400,'暂不支持该城市，请从演示城市列表选择。')
    return value

def validate_trip(body):
    value=body.model_dump(mode='json')
    value['origin'],value['destination']=city(body.origin),city(body.destination)
    today=clock().date()
    if value['origin']==value['destination']:raise HTTPException(400,'出发地和目的地不能相同。')
    if not today<body.departure_date<=today+timedelta(days=180):
        raise HTTPException(400,'演示出行日期请选择明天至未来180天。')
    if not body.departure_date<body.return_date<=body.departure_date+timedelta(days=30):
        raise HTTPException(400,'返程/退房日期需晚于出发日期，最多入住30晚。')
    matches=[a for a in CITIES[value['destination']] if body.area in a or a.removesuffix('区') in body.area]
    if body.area not in {'不限','任意区域','全市'} and len(matches)!=1:
        raise HTTPException(400,'该目的地可选住宿区域：'+'、'.join(CITIES[value['destination']])+'；也可明确选择不限。')
    value['area']=matches[0] if matches else '不限'
    return value

def inventory(trip):
    """Generate a small reproducible catalogue, with alternatives for each leg."""
    def services(origin,destination,day,leg):
        seed=int(sha256((origin+destination+day).encode()).hexdigest()[:8],16)
        result=[]
        for mode,prefix,hour,base,duration in [('flight','演示航班',9,420,145),('flight','演示航班',14,620,160),('train','演示动车',8,220,260),('train','演示动车',13,320,280)]:
            code=f'{leg}-{mode}-{hour}'
            minutes=duration+seed%60
            start=datetime.fromisoformat(day+f'T{hour:02}:00:00')
            end=start+timedelta(minutes=minutes)
            result.append({'id':code,'kind':mode,'name':prefix+str(seed%800+100)+('A' if hour<12 else 'B'),
                'origin':origin,'destination':destination,'date':day,'departure':start.strftime('%H:%M'),
                'arrival':end.strftime('%H:%M'),'duration_minutes':minutes,'class_name':'经济舱' if mode=='flight' else '二等座',
                'price_minor':(base+seed%120)*100,'quantity':trip['passengers'],'available':True})
        return result
    nights=(datetime.fromisoformat(trip['return_date'])-datetime.fromisoformat(trip['departure_date'])).days
    areas=CITIES[trip['destination']] if trip['area']=='不限' else [trip['area']]
    hotels=[]
    for a in areas:
        for index,(label,base) in enumerate([('轻居',229),('雅居',369),('云景',589)]):
            hotels.append({'id':f"hotel-{CITIES[trip['destination']].index(a)}-{index}",'kind':'hotel',
                'name':trip['destination']+a+label+'演示酒店','area':a,'address':a+'演示路'+str(index+1)+'号',
                'room_type':'标准双床房','price_minor':base*100,'quantity':trip['rooms'],'nights':nights,
                'checkin':trip['departure_date'],'checkout':trip['return_date'],'rating':4.5+index/10,'available':True})
    return {'trip':trip,'outbound':services(trip['origin'],trip['destination'],trip['departure_date'],'out'),
        'inbound':services(trip['destination'],trip['origin'],trip['return_date'],'back'),'hotels':hotels,
        'simulated':True,'source':'独立旅行演示API，全部班次、酒店和价格均为虚构'}
