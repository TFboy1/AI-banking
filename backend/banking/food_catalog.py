"""Entirely fictional restaurants, addresses and prices, never real merchant data."""
from math import asin, cos, radians, sin, sqrt

PLACES = [
    {'id': 'lujiazui', 'name': '上海·陆家嘴（演示位置）', 'address': '演示地址：陆家嘴测试大楼1层', 'lat': 31.2397, 'lng': 121.4998},
    {'id': 'people_square', 'name': '上海·人民广场（演示位置）', 'address': '演示地址：人民广场测试大楼1层', 'lat': 31.2304, 'lng': 121.4737},
    {'id': 'xujiahui', 'name': '上海·徐家汇（演示位置）', 'address': '演示地址：徐家汇测试大楼1层', 'lat': 31.1880, 'lng': 121.4365},
]
SHOPS = []
for zone, place in enumerate(PLACES):
    for index, (name, category, icon, dishes) in enumerate([
        ('小满饭堂', '米饭 家常菜', '🍱', [('招牌黄焖鸡饭', 2600), ('番茄牛腩饭', 3200), ('宫保鸡丁饭', 2400), ('清炒时蔬', 1200), ('米饭', 300), ('冰柠檬茶', 800)]),
        ('巷口面馆', '面条 牛肉面 饺子', '🍜', [('招牌牛肉面', 2800), ('番茄鸡蛋面', 1800), ('鲜肉水饺', 2200), ('卤蛋', 300), ('凉拌黄瓜', 1000), ('酸梅汤', 600)]),
        ('快乐汉堡', '汉堡 炸鸡 奶茶 咖啡', '🍔', [('香辣鸡腿堡', 1900), ('双层牛肉堡', 2900), ('薯条', 1000), ('炸鸡双拼', 2500), ('珍珠奶茶', 1200), ('美式咖啡', 1200)]),
        ('甜予烘焙', '生日 蛋糕 甜品', '🎂', [('六寸水果生日蛋糕', 13800), ('八寸奶油生日蛋糕', 19800), ('六寸低糖生日蛋糕', 16800)]),
    ]):
        code = f'{place["id"]}_{index}'
        SHOPS.append({'id': code, 'name': name + '·' + place['name'].split('·')[1].split('（')[0] + '店', 'category': category, 'icon': icon,
                      'lat': place['lat'] + .002 * (index+1), 'lng': place['lng'] + .002 * (index+1),
                      'rating': [4.8, 4.7, 4.9, 4.8][index], 'minutes': 25 + index*5, 'minimum_minor': 1500,
                      'delivery_minor': 300 + index*100, 'radius_km': 4,
                      'menu': [{'id': code+'_'+str(i), 'name': dish, 'price_minor': price, 'packing_minor': 100, 'available': True} for i, (dish, price) in enumerate(dishes)]})


def distance(place, shop):
    a, b = radians(place['lat']), radians(shop['lat'])
    d = sin((b-a)/2)**2 + cos(a)*cos(b)*sin(radians(shop['lng']-place['lng'])/2)**2
    return round(6371 * 2 * asin(sqrt(d)), 2)
