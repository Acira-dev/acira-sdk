"""Target selection advertises readiness only after document and render setup."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('ready_state', ['loading', 'interactive', 'complete'])
def test_selection_readiness_waits_for_document_and_two_frames(ready_state):
    node = shutil.which('node')
    assert node, 'Node is required for extension readiness regression checks'
    selection = Path(__file__).parents[1]/'tools/acira-capture-extension/selection.js'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const sent=[],frames=[],handlers={},documentHandlers={};
const document={readyState:process.argv[2],body:{focus(){}},
 addEventListener:(name,callback,options)=>{assert.equal(options.once,true);assert.equal(Object.keys(options).length,1);documentHandlers[name]=callback}};
const window={addEventListener:(name,callback)=>{handlers[name]=callback}};
const chrome={runtime:{connect:()=>({postMessage:message=>sent.push(message)})}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),
 {document,window,chrome,location:{hash:'#'+'a'.repeat(48)},
  requestAnimationFrame:callback=>frames.push(callback),innerHeight:720});
assert.equal(sent.length,0);
for(const name of ['mousemove','pointerdown','click','wheel','keydown'])assert.equal(typeof handlers[name],'function');
if(document.readyState==='loading'){
 assert.equal(frames.length,0);
 document.readyState='interactive';documentHandlers.DOMContentLoaded();
}else assert.equal(documentHandlers.DOMContentLoaded,undefined);
assert.equal(frames.length,1);frames.shift()();assert.equal(sent.length,0);
assert.equal(frames.length,1);frames.shift()();
assert.deepEqual(JSON.parse(JSON.stringify(sent)),[{type:'acira.selection',kind:'ready'}]);
assert.equal(frames.length,0);
handlers.click({isTrusted:false,clientX:20,clientY:30,preventDefault(){}});
assert.equal(sent.length,1);
handlers.click({isTrusted:true,clientX:20,clientY:30,preventDefault(){}});
assert.deepEqual(JSON.parse(JSON.stringify(sent.at(-1))),{type:'acira.selection',kind:'select',x:20,y:30});
assert.equal(sent.length,2);
'''
    result = subprocess.run([node, '-e', script, str(selection), ready_state],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
