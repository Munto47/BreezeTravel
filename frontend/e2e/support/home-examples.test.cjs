const {test}=require('node:test')
const assert=require('node:assert/strict')
const fs=require('node:fs')
const vm=require('node:vm')
const ts=require('typescript')
test('home examples describe twelve mainline stops within the short streaming limit',()=>{
 const source=fs.readFileSync('src/components/experience/home-examples.ts','utf8')
 const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText
 const context={exports:{}};vm.runInNewContext(compiled,context)
 const examples=context.exports.HOME_EXAMPLES
 assert.equal(examples.length,2)
 for(const example of examples){
  assert.equal(example.days.length,3)
  assert.equal(example.days.flat().length,12)
  assert.ok(example.days.every(day=>day.length===4))
  assert.ok([...example.text].length<=500)
  for(const name of example.days.flat())assert.ok(example.text.includes(name),name)
  assert.match(example.text,/备选/);assert.match(example.text,/取消/);assert.match(example.label,/12 个主线地点/)
 }
})
