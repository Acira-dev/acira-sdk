"""The selector bridge authenticates Chrome senders and isolates tabs/instances."""
import shutil
import subprocess
from pathlib import Path


def test_selection_bridge_rejects_wrong_senders_and_routes_only_matching_instances():
    node = shutil.which("node")
    assert node, "Node is required to verify the packaged extension bridge"
    background = Path(__file__).parents[1] / "tools/acira-capture-extension/background.js"
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
let connect;
const chrome={action:{onClicked:{addListener(){}}},
 runtime:{id:'fixture-extension',getURL:p=>'chrome-extension://fixture-extension/'+p,
 onConnect:{addListener:f=>connect=f},onMessage:{addListener(){}}}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{chrome,Map,Number,URL});
const token='a'.repeat(48),other='b'.repeat(48);
function port(role,nonce=token,tab=1,change={}){
 const sender={id:chrome.runtime.id,tab:{id:tab},frameId:role==='controller'?0:2,
  url:role==='controller'?'https://fixture.invalid/':chrome.runtime.getURL('selection.html')+'#'+nonce,
  ...change};
 const p={name:'acira.selection.'+role+'.'+nonce,sender,received:[],closed:false,
  onMessage:{addListener:f=>p.receive=f},onDisconnect:{addListener:f=>p.disconnected=f},
  postMessage:m=>p.received.push(m),disconnect(){this.closed=true;this.disconnected?.()}};
 connect(p);return p;
}
for(const [role,change] of [
 ['controller',{id:'other-extension'}],['controller',{tab:undefined}],
 ['controller',{frameId:2}],['controller',{url:'chrome://settings'}],
 ['frame',{frameId:0}],['frame',{url:'https://fixture.invalid/selection.html'}],
 ['frame',{url:chrome.runtime.getURL('selection.html')+'#'+other}]]){
 assert.equal(port(role,token,1,change).closed,true);
}
const first=port('controller'),second=port('controller',token,2);
const frame=port('frame');
frame.receive({type:'acira.selection',kind:'ready'});
assert.equal(first.received.at(-1).kind,'ready');
assert.equal(second.received.length,0);
const another=port('controller',other),anotherFrame=port('frame',other);
frame.receive({type:'acira.selection',kind:'select',x:40,y:50,secret:'must not forward'});
assert.equal(first.received.at(-1).kind,'select');
assert.equal('secret' in first.received.at(-1),false);
assert.equal(another.received.length,0);
assert.equal(port('frame').closed,true);
const count=first.received.length;
frame.receive({type:'acira.selection',kind:'approve'});
first.receive({type:'acira.selection',kind:'select',x:1,y:1});
assert.equal(first.received.length,count);
frame.disconnect();
assert.equal(first.received.at(-1).kind,'unavailable');
const waitingFrame=port('frame','c'.repeat(48),3);
waitingFrame.receive({type:'acira.selection',kind:'ready'});
const lateController=port('controller','c'.repeat(48),3);
assert.equal(lateController.received.at(-1).kind,'ready');
"""
    result = subprocess.run([node, "-e", script, str(background)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
