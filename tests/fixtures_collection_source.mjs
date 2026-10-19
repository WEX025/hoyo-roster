export function record(game, source) {
  const operation = source.split(":")[1];
  const value = operation === "identity" ? {uid:"123456789",region:"Synthetic",nickname:"Synthetic"} :
    operation === "roster" ? {rows:[{name:"Alpha",level:"Lv.50"}]} : {details: game === "genshin" ? [{
      name:"Alpha",synced:true,levels:["50","90"],weapon:{name:"Synthetic",levels:["20","90"]},
      skills:["普通攻击","元素战技","元素爆发"].map(kind=>({labels:[kind,"Synthetic"],levels:["1","10"]})),
    }] : [{name:"Alpha",display_name:"Alpha",current_weapon:"EMPTY",current_skills:"当前技能等级\nSynthetic",current_discs:"当前装备驱动盘\n尚未装备驱动盘"}]};
  return {json:JSON.stringify(value),origin:{kind:"official-page",game,
    url:game === "genshin" ? "https://act.mihoyo.com/ys/event/calculator/index.html#/character" : "https://act.mihoyo.com/zzz/gt/character-builder-h/index.html#/"},
    evidence:{surface:operation === "identity" ? "role-selector" : operation === "roster" ? "owned-selector" : game === "genshin" ? "character-selection-and-editor" : "current-agent-sections",generation:0}};
}
