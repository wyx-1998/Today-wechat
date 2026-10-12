import test from "node:test";
import assert from "node:assert/strict";
import {emptyMessageFilter, messageQuery} from "../src/message-query.ts";
test("empty filter does not change data range or sender",()=>assert.deepEqual(messageQuery(emptyMessageFilter()),{}));
test("sender ids are distinct from the unknown option and never matched by nickname",()=>{
  assert.deepEqual(messageQuery({from:"",to:"",sender:"id:unknown:"}),{sender_id:"unknown:"});
  assert.deepEqual(messageQuery({from:"",to:"",sender:"unknown:"}),{sender_unknown:true});
});
test("date bounds include the ending local calendar day",()=>{
  const result=messageQuery({from:"2026-10-02",to:"2026-10-02",sender:""});
  assert.equal(new Date(result.time_start).getDate(),2);
  assert.equal(new Date(result.time_end).getDate(),3);
  assert.equal(new Date(result.time_end).getHours(),0);
});
test("one-sided dates are allowed and malformed or reversed dates are refused",()=>{
  assert.ok(messageQuery({from:"2026-10-02",to:"",sender:""}).time_start);
  assert.ok(messageQuery({from:"",to:"2026-10-02",sender:""}).time_end);
  for(const filter of [{from:"2026-10-03",to:"2026-10-02",sender:""},{from:"2026-02-30",to:"",sender:""},{from:"",to:"bad",sender:""}]) assert.throws(()=>messageQuery(filter));
});
test("daylight savings uses next midnight rather than assuming 24 hours",()=>{
  const previous=process.env.TZ;
  try {process.env.TZ="America/New_York";const result=messageQuery({from:"2026-03-08",to:"2026-03-08",sender:""});assert.equal(Date.parse(result.time_end)-Date.parse(result.time_start),23*3600000);}
  finally {if(previous===undefined)delete process.env.TZ;else process.env.TZ=previous;}
});
