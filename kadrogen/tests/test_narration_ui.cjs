const { chromium }=require('playwright');
(async()=>{
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH || undefined,args:['--no-sandbox']});
const page=await browser.newPage({viewport:{width:1280,height:900}});const errors=[];
page.on('pageerror',e=>errors.push(e.message));
await page.goto(require('url').pathToFileURL(require('path').resolve(__dirname,'../ui.html')).href);
await page.evaluate(()=>{
window.pywebview={api:{save_config:async()=>{},pick_narration_tracks:async()=>['C:/voice/act1.wav','C:/voice/act2.wav'],pick_narration_timing:async()=> 'C:/voice/timing.csv'}};
document.querySelectorAll('input[name=prov]').forEach(e=>e.checked=e.value==='manual');go(4);renderAccess();
});
await page.locator('#narration_add').click();
if(await page.locator('#narration_list>div').count()!==2)throw Error('missing list');
await page.locator('[aria-label="Narration down"]').first().click();
let sent=await page.evaluate(()=>opts());
if(sent.narration.tracks[0]!=='C:/voice/act2.wav'||sent.audio!==sent.narration.tracks[0])throw Error('order');
await page.locator('#narration_mode').selectOption('timing');
await page.locator('#narration_timing').click();
sent=await page.evaluate(()=>opts());
if(sent.narration.mode!=='timing'||sent.narration.timing!=='C:/voice/timing.csv')throw Error('timing options');
await page.screenshot({path:'narration-ui.png'});
await page.locator('[aria-label="Remove narration"]').first().click();
if(await page.locator('#narration_list>div').count()!==1)throw Error('remove');
await page.evaluate(()=>setAudio('C:/single.wav'));
if((await page.evaluate(()=>opts())).narration!==null)throw Error('single audio reset');
await page.evaluate(async()=>{
  buildStages('images');
  if ($('sg_scenes').textContent !== 'Подготовка кадров' || $('stages').textContent.includes('undefined')) throw Error('missing stage label');
  pywebview.api.get_state=async()=>({running:false,stage:'render',done:1,total:3,elapsed:1,scenes:3,log:[]});
  pywebview.api.gallery=async()=>({total:3,items:[{i:0,key:0,img:'data:image/png;base64,AA=='},{i:1,key:2,img:'data:image/png;base64,AA=='}]});
  buildFrames(3);
  await poll();
  buildFrames(3); // restart before delayed thumbnails from the old poll arrive
  await new Promise(resolve=>setTimeout(resolve,220));
  if($('frames').querySelector('img'))throw Error('old thumbnails leaked into restarted gallery');
});
if(errors.length)throw Error(errors.join('\n'));
console.log('Narration UI passed: add, reorder, timing, remove, single-file reset');await browser.close();
})();

