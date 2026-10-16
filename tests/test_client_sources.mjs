// The browser network boundary is synthetic. Game readers, host, worker,
// normalization and disk transactions are the actual production implementations.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { runInNewContext } from "node:vm";
import { createRosterHost } from "../src/hoyo_roster/client_runtime.mjs";
import { createClientNetwork, requireReceipt, sourceStep } from "../src/hoyo_roster/client_network.mjs";
import { generateDs } from "../src/hoyo_roster/games/zzz/client_source.mjs";

const root = resolve(import.meta.dirname, "..");
const python = join(root, ".venv", process.platform === "win32" ? "Scripts" : "bin", process.platform === "win32" ? "python.exe" : "python");
const fixtures = JSON.parse(await readFile(new URL("./client-sources.json", import.meta.url), "utf8"));
const GS = "https://act.mihoyo.com/ys/event/calculator/index.html#/character";
const COMMUNITY = "https://www.miyoushe.com/zzz/";
const BADGE = "https://api-takumi.mihoyo.com/common/badge/v1/login/account";
const request = (id, method, session, params = {}) => ({ version:1,id,method,session,params });

function browserNetworkFixture({ cdpAvailable = true, authenticated = true } = {}) {
  const tabs = new Map(), events = [], bodies = new Map(), calls = [];
  let nextTab = 0, nextRequest = 0, initialized = false, headerPrimes = 0;
  let rawOverride = null, eventGap = false, nextRedirect = false, nextBlocked = false, nextBase64 = false, gsRequest = null;
  const emit = (method, params) => events.push({ method,params,sequence:events.length+1,source:{} });
  function response(url, options, raw = null) {
    const id = String(++nextRequest);
    calls.push({url,options});
    emit("Network.requestWillBeSent", {requestId:id,request:{url,method:options.method,
      ...(options.body === undefined ? {} : {postData:options.body})},
      ...(nextRedirect ? {redirectResponse:{status:302}} : {})});
    nextRedirect = false;
    if (nextBlocked) {
      nextBlocked = false;
      emit("Network.loadingFailed",{requestId:id,blockedReason:"inspector"});
      return;
    }
    let record;
    if (url.startsWith(BADGE)) {
      initialized = true;
      raw ??= '{"retcode":0,"data":{"token":"synthetic-private-receipt"}}';
      record = {json:raw,origin:{status:200}};
    } else {
      record = Object.values(fixtures).flatMap(game=>Object.values(game)).find(value=>url.startsWith(value.origin.url));
      assert.ok(record,"Unregistered synthetic network request");
      if (!authenticated || (url.includes("nap_cultivate_tool") && !initialized)) {
        raw = '{"retcode":-100,"data":{}}';
      }
    }
    bodies.set(id,{body:raw ?? rawOverride ?? record.json,base64Encoded:nextBase64});
    if (nextBase64) bodies.get(id).body = Buffer.from(bodies.get(id).body).toString("base64");
    nextBase64 = false;
    emit("Network.responseReceived",{requestId:id,response:{url,status:record.origin.status,fromDiskCache:false,fromServiceWorker:false}});
    emit("Network.loadingFinished",{requestId:id});
  }
  function makePage(url) {
    const id = String(++nextTab);
    let enabled = false;
    const page = {
      id, current:url, markHandoff:async()=>{}, markDeliverable:async()=>{},
      url:async()=>page.current,
      goto:async target=>{
        page.current = target;
        if (!enabled) return;
        if (target === GS) {
          response(fixtures.genshin["cultivation-avatar-list"].origin.url,
            {method:"POST",body:JSON.stringify(gsRequest ?? fixtures.genshin["cultivation-avatar-list"].evidence.request)});
        } else if (target === COMMUNITY) {
          headerPrimes++;
          const headers={"x-rpc-device_id":"synthetic-device","x-rpc-device_fp":"synthetic-fp"};
          Object.defineProperty(headers,"Cookie",{enumerable:true,get(){throw new Error("Cookie value must not be read");}});
          emit("Network.requestWillBeSent",{requestId:"prime-"+headerPrimes,request:{
            url:"https://api-takumi.mihoyo.com/synthetic-public-bootstrap",method:"GET",
            headers,
          }});
        }
      },
      reload:async()=>page.goto(page.current), close:async()=>tabs.delete(id),
      capabilities:{
        list:async()=>cdpAvailable ? [{id:"cdp"}] : [],
        get:async capability=>{
          assert.equal(capability,"cdp");
          assert.ok(cdpAvailable,"An unavailable capability was accessed");
          return {
            send:async(method,params)=>{
              calls.push({command:method});
              if (method === "Network.enable") {enabled=true;return {};}
              if (method === "Network.disable") {enabled=false;return {};}
              if (method === "Network.getResponseBody") return bodies.get(params.requestId);
              if (method === "Runtime.evaluate") {
                runInNewContext(params.expression,{fetch:(target,options)=>{
                  assert.equal(options.credentials,"include");
                  assert.equal(options.redirect,"error");
                  assert.ok(!Object.keys(options.headers).some(name=>/^(cookie|authorization|origin|referer)$/i.test(name)));
                  response(target,options);
                  return Promise.resolve();
                }});
                return {result:{type:"undefined"}};
              }
              throw new Error("Unexpected browser command");
            },
            readEvents:async options=>{
              if (options.afterSequence === undefined) return {cursor:events.length,events:[],hasMore:false,truncated:false};
              const selected=events.filter(event=>event.sequence>options.afterSequence&&options.methods.includes(event.method));
              if (!selected.length) await new Promise(resolve=>setTimeout(resolve,20));
              return {cursor:events.length,events:selected,hasMore:false,truncated:eventGap};
            },
          };
        },
      },
    };
    tabs.set(id,page);
    return page;
  }
  const gs=makePage(GS);
  const browser={tabs:{list:async()=>[...tabs.values()].map(page=>({id:page.id,url:page.current})),get:async id=>tabs.get(id),new:async()=>makePage("about:blank")},capabilities:{list:async()=>[]}};
  return {browser,gs,calls,setAuthenticated:value=>{authenticated=value;},
    raw:value=>{rawOverride=value;},gap:()=>{eventGap=true;},redirect:()=>{nextRedirect=true;},
    gsRequest:value=>{gsRequest=value;},
    blocked:()=>{nextBlocked=true;},base64:()=>{nextBase64=true;},headerPrimes:()=>headerPrimes};
}

test("Raw official response text survives long numeric literals and base64",async()=>{
  const fixture=browserNetworkFixture();
  const network=createClientNetwork(fixture.browser);
  const signal=new AbortController().signal;
  const raw='{"retcode":0,"data":{"value":0.12345678901234567890123456789}}';
  fixture.raw(raw);fixture.base64();
  const result=await network.request(fixture.gs,{url:fixtures.genshin.binding.origin.url,method:"GET"},signal,{params:{game_biz:"hk4e_cn"}});
  assert.equal(result.json,raw);
  assert.equal(result.origin.method,"GET");
  assert.equal(result.origin.redirected,false);
});

test("A game keeps its tab across same-origin routes and rejects cross-origin replacement",async()=>{
  const fixture=browserNetworkFixture();
  const network=createClientNetwork(fixture.browser),signal=new AbortController().signal;
  const original=await network.pageFor(GS,signal);
  original.current="https://act.mihoyo.com/another-official-route";
  assert.equal((await network.pageFor(GS,signal)).id,original.id);
  original.current="https://example.invalid/replaced-page";
  await assert.rejects(network.pageFor(GS,signal),{code:"SOURCE_DATA_INVALID"});
  assert.deepEqual(fixture.calls,[]);
});

test("Donor DS vector agrees with the original Python protocol",()=>{
  assert.equal(generateDs({timestamp:1700000000,nonce:123456,params:{server:"prod_gf_cn",role_id:"123456789"}}),
    "1700000000,123456,ec4a22dc58c688b14ac78abcd9f46d69");
});

for (const failure of ["gap","redirect","blocked"]) test("Observation rejects "+failure,async()=>{
  const fixture=browserNetworkFixture();fixture[failure]();
  const network=createClientNetwork(fixture.browser);
  await assert.rejects(network.request(fixture.gs,{url:fixtures.genshin.binding.origin.url,method:"GET"},new AbortController().signal),
    {code:failure==="blocked"?"CLIENT_SOURCE_UNAVAILABLE":"SOURCE_DATA_INVALID"});
});

for (const json of ['{"retcode":0,"retcode":1034,"data":{}}','{"retcode":true,"data":{}}','{"retcode":0.0,"data":{}}','{"retcode":0,"data":{"a":1,"a":2}}']) {
  test("Ambiguous service receipt is rejected: "+json.slice(0,24),()=>{
    assert.throws(()=>requireReceipt({json,origin:{status:200,redirected:false}}),{code:"SOURCE_DATA_INVALID"});
  });
}

test("Source deadline bounds hung SDK waits and prevents late browser actions",async()=>{
  let finish,lateActions=0;
  const hanging=new Promise(resolve=>{finish=resolve;});
  await assert.rejects(sourceStep({signal:new AbortController().signal},async({signal})=>{
    await hanging;
    signal.throwIfAborted();
    lateActions++;
  },25),{code:"SOURCE_REQUEST_NOT_OBSERVED"});
  finish();
  await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(lateActions,0);
});

for (const mutation of ["filter","page","identity","size"]) test("Genshin rejects observed request with wrong "+mutation,async()=>{
  const fixture=browserNetworkFixture();
  const body=structuredClone(fixtures.genshin["cultivation-avatar-list"].evidence.request);
  if(mutation==="filter")body.element_attr_ids=[3];
  if(mutation==="page")body.page=2;
  if(mutation==="identity")body.uid="987654321";
  if(mutation==="size")body.size=true;
  fixture.gsRequest(body);
  const host=createRosterHost(fixture.browser);
  const attached=await host.exchange(request(1,"session.attach",null));
  const result=await host.exchange(request(2,"source.read",attached.session,{game:"genshin",source:"cultivation-avatar-list",params:{uid:"123456789",region:"cn_gf01"},body:null}));
  assert.equal(result.error,"SOURCE_DATA_INVALID");
});

test("Unadvertised capabilities cause no CDP commands, network calls or replacement browser",async()=>{
  const fixture=browserNetworkFixture({cdpAvailable:false});
  const host=createRosterHost(fixture.browser);
  const attach=await host.exchange(request(1,"session.attach",null));
  const checked=await host.exchange(request(2,"source.check",attach.session,{game:"genshin",sources:["binding","cultivation-avatar-list"]}));
  assert.deepEqual(checked.result,{available:[]});
  assert.deepEqual(fixture.calls,[]);
});

test("Service receipt strips auth data and device headers are refreshed after login",async()=>{
  const fixture=browserNetworkFixture();
  const host=createRosterHost(fixture.browser);
  const attach=await host.exchange(request(1,"session.attach",null));
  const prepared=await host.exchange(request(2,"service.prepare",attach.session,
    {game:"zzz",service:"cultivate",params:{uid:"123456789",region:"prod_gf_cn"}}));
  assert.equal(prepared.result.ready,true);
  assert.ok(!JSON.stringify(prepared).includes("synthetic-private"));
  assert.equal(fixture.headerPrimes(),1);
  const opened=await host.exchange(request(3,"login.open",attach.session,{url:GS}));
  const confirming=host.exchange(request(4,"login.confirm",attach.session,{page:opened.result.page,question:"Synthetic user confirmation"}));
  while(!host.pendingQuestion()) await new Promise(resolve=>setTimeout(resolve,1));
  await host.answerLogin("completed",host.pendingQuestion().attempt);
  await confirming;
  const binding=await host.exchange(request(5,"source.read",attach.session,{game:"zzz",source:"binding",params:{game_biz:"nap_cn"},body:null}));
  assert.equal(binding.error,null);
  assert.equal(fixture.headerPrimes(),2);
});

test("Bundled donor readers drive raw browser events through real worker and both saved snapshots",{timeout:120000},async()=>{
  const fixture=browserNetworkFixture({authenticated:false});
  const temporary=await realpath(await mkdtemp(join(tmpdir(),"hoyo-donor-network-")));
  const host=createRosterHost(fixture.browser);
  const task=host.startTask({python,cwd:root,runtimeRoot:join(temporary,"runtime"),storageRoot:join(temporary,"results")});
  try {
    for(let i=0;i<1000&&!host.pendingQuestion()&&!task.status().done;i++) await task.pump({waitMs:25,maxRequests:1});
    assert.ok(host.pendingQuestion(),JSON.stringify(task.status()));
    fixture.setAuthenticated(true);
    await host.answerLogin("completed",host.pendingQuestion().attempt);
    while(!task.status().done) await task.pump({waitMs:100,maxRequests:1});
    const result=await task.completion;
    assert.equal(result.code,0,result.events.join("\n"));
    assert.equal(result.events.filter(line=>line.startsWith("CURRENT_RESULT=")).length,2);
    assert.equal(result.events.filter(line=>line==="PROGRESS=LOGIN_COMPLETED").length,1);
    assert.ok(result.events.includes("TASK_STATUS=PASS"));
    const badge=fixture.calls.filter(call=>call.url?.startsWith(BADGE));
    assert.equal(badge.length,1);
    assert.equal(badge[0].options.headers["x-rpc-device_id"],undefined);
    assert.ok(fixture.calls.some(call=>call.url?.includes("id_list%5B%5D=")));
    assert.ok(!result.events.some(line=>/synthetic-private|123456789|987654321/.test(line)));
  } finally {
    if(!task.status().done){task.cancel();await task.completion;}
    await rm(temporary,{recursive:true,force:true});
  }
});
