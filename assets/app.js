const POPULATION=40, GENERATION_LENGTH=10;
let generation=0, history=[];

function randomIndividual(){
  // A simple abstract gene: 8-bit bias for an adder-like lookup behavior.
  return {genes:Array.from({length:256},()=>Math.random()<.5?0:1), mutations:0};
}

function candidateFitness(c){
  let score=0;
  for(let a=0;a<256;a+=17){
    for(let b=0;b<256;b+=17){
      const address=(a+b)&255;
      const actual=c.genes[address] ? ((a+b)&255) : 0;
      if(actual===((a+b)&255)) score++;
    }
  }
  return score/256;
}

let population=Array.from({length:POPULATION},randomIndividual);

function tournament(){
  const pool=[];
  while(pool.length<5){const x=population[Math.floor(Math.random()*population.length)];if(!pool.includes(x))pool.push(x)}
  return pool.sort((a,b)=>candidateFitness(b)-candidateFitness(a))[0];
}

function crossover(a,b){
  const cut=Math.floor(Math.random()*a.genes.length);
  return {genes:[...a.genes.slice(0,cut),...b.genes.slice(cut)],mutations:0};
}

function mutate(c){
  const count=1+Math.floor(Math.random()*3);
  for(let i=0;i<count;i++){const p=Math.floor(Math.random()*c.genes.length);c.genes[p]^=1;c.mutations++}
  return c;
}

function evolveGeneration(){
  population.sort((a,b)=>candidateFitness(b)-candidateFitness(a));
  const next=population.slice(0,8);
  while(next.length<POPULATION) next.push(mutate(crossover(tournament(),tournament())));
  population=next; generation++; update();
}

function update(){
  population.sort((a,b)=>candidateFitness(b)-candidateFitness(a));
  const best=candidateFitness(population[0]);
  history.push(best);
  document.querySelector('#generation').textContent=generation;
  document.querySelector('#fitness').textContent=Math.round(best*100)+'%';
  document.querySelector('#population').innerHTML=population.map(c=>`<div class="individual" title="${Math.round(candidateFitness(c)*100)}%"><i style="width:${candidateFitness(c)*100}%"></i></div>`).join('');
  draw();
}

function draw(){
  const canvas=document.querySelector('#chart'),ctx=canvas.getContext('2d');
  ctx.clearRect(0,0,canvas.width,canvas.height);ctx.strokeStyle='#ddd7c7';ctx.lineWidth=1;
  for(let y=0;y<=4;y++){const yy=20+(canvas.height-40)*y/4;ctx.beginPath();ctx.moveTo(45,yy);ctx.lineTo(880,yy);ctx.stroke()}
  ctx.fillStyle='#637084';ctx.font='12px sans-serif';ctx.fillText('100%',4,24);ctx.fillText('0%',18,222);
  if(history.length<2)return;
  ctx.strokeStyle='#147d78';ctx.lineWidth=4;ctx.beginPath();
  history.forEach((value,i)=>{const x=45+(history.length===1?0:i*(835/(history.length-1)));const y=220-value*(190);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke();
}

document.querySelector('#run').addEventListener('click',()=>{for(let i=0;i<GENERATION_LENGTH;i++)evolveGeneration()});
update();
