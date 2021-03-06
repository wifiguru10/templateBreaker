#!/usr/bin/ipython3 -i

from unittest import result
from async_timeout import timeout
import meraki
import copy
import asyncio
import os
from time import *

from meraki import aio
from requests import post, session
import tqdm.asyncio

#import time
import get_keys as g
import datetime
import random

import click

import aiohttp
import time

#Main dashboard object
db = meraki.DashboardAPI(
            api_key=g.get_api_key(), 
            base_url='https://api.meraki.com/api/v1/', 
            output_log=True,
            log_file_prefix=os.path.basename(__file__)[:-3],
            log_path='Logs/',
            print_console=False)


targetORG = '123412341234'


networkTAG_UNBIND = 'UNBIND_ME_Group1'
networkTAG_DONE = networkTAG_UNBIND + '_DONE'

start_time = time.time()

def getPOSTurlNetid(netID):
    return f"https://api.meraki.com/api/v1/networks/{str(netID)}/unbind?retainConfigs=True"

async def post_unbindNetwork(aiosess, netID):
    #payload = {'retainConfigs': 'True'}
    url = getPOSTurlNetid(netID)
    headers = {
        'Accept': "*/*",
        'Content-Type': "application/json",
        'cache-control': "no-cache",
        "X-Cisco-Meraki-API-Key" : g.get_api_key()    
    }
    
    #return
    
    instance_time = time.time()

    result = f"Network[{netID}] - "
    print(f"NetID[{netID}] queued......")
    async with aiosess.post(url, headers=headers) as resp:
        result = result + await resp.text()

    diffTime = time.time() - instance_time
    result = result + (f" Net[{netID}] Comleted[{diffTime}]seconds")
    print(result)

    if not "error" in result:
        netTemp = db.networks.getNetwork(netID)
        if not netTemp['isBoundToConfigTemplate']:
            net = db.networks.getNetwork(netID)
            tags = net['tags']
            if networkTAG_UNBIND in tags:
                tags.remove(networkTAG_UNBIND)
            tags.append(networkTAG_DONE)
            db.networks.updateNetwork(netID, tags = tags)
            print(f"Network[{net['name']}] NetID[{net['id']}] Completed!!!")
            result = result + f"Network[{net['name']}] NetID[{net['id']}] Completed!!!"
            return result
    else:
        print(f"FAILURE on Network[{netTemp['name']}] NetId[{netTemp['id']}]")
        print(f"\tRESULT: {result}")
        result = result + f"  FAILURE on Network[{netTemp['name']}] NetId[{netTemp['id']}]"
        return result
        
    print(f"Completed without return/result......")
    return result

async def main():

    nets = db.organizations.getOrganizationNetworks(targetORG)
    targetNets = []
    for n in nets:
        if networkTAG_UNBIND in n['tags']:
            if n['isBoundToConfigTemplate']:
                targetNets.append(n)

    count = 0
    if len(targetNets) > count:
        count = len(targetNets)
    print()
    print(f"Found {len(targetNets)} networks in scope using tag[{networkTAG_UNBIND}] ")

    timeout_obj = aiohttp.ClientTimeout(6000)
    async with aiohttp.ClientSession(timeout=timeout_obj) as aiosess:

        tasks = []
        for t in targetNets:
            tasks.append(asyncio.ensure_future(post_unbindNetwork(aiosess,t['id'])))
            print(t)

        print()
        print("AWAITING.....")
        print()

    
        print(f"\Results:")

        for task in tqdm.tqdm(asyncio.as_completed(tasks), total=len(tasks), colour='green'):
        #unbound = await asyncio.gather(*tasks)
            print(await task)
            #await task

    
        #print(f"\nPrinting results.....")
        #for r in unbound:
        #    print(r)

    print()
    print(f"DONE")

asyncio.run(main())
print("--- %s seconds ---" % (time.time() - start_time))
