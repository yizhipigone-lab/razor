// Fix main.js using line-based approach
const fs = require('fs');
const file = 'e:/1target/p9_project/quant-platform/static/js/main.js';
const lines = fs.readFileSync(file, 'utf8').split('\n');

// Find and replace the trade table rendering section
// Search for the pattern in the override renderSimpleBtResults
let tradeStart = -1, tradeEnd = -1;
for (let i = 0; i < lines.length; i++) {
  if (lines[i].includes("document.getElementById('simple-bt-trade-count').textContent") &&
      lines[i].includes("trades.length")) {
    tradeStart = i;
    // Find the end of the tbody.innerHTML assignment (ends with .join('');)
    for (let j = i + 1; j < Math.min(i + 15, lines.length); j++) {
      if (lines[j].includes(".join('');") && !lines[j].includes('slice')) {
        tradeEnd = j;
        break;
      }
    }
    console.log(`Trade section: lines ${tradeStart+1}-${tradeEnd+1}`);
    break;
  }
}

if (tradeStart >= 0 && tradeEnd > tradeStart) {
  const newLines = [
    "  document.getElementById('simple-bt-trade-count').textContent = '(' + trades.length + '笔)';",
    "  const tbody = document.getElementById('simple-bt-tbody');",
    "  tbody.innerHTML = trades.slice(-200).reverse().map(t =>",
    "    '<tr>'+",
    "    '<td>'+t.code+'</td><td>'+(t.name||'')+'</td><td>'+(t.shares||0)+'</td>'+",
    "    '<td>'+t.entry_date+'</td><td>'+t.entry_px+'</td><td>'+(t.entry_total||0)+'</td>'+",
    "    '<td>'+t.exit_date+'</td><td>'+t.exit_px+'</td><td>'+(t.exit_total||0)+'</td>'+",
    "    '<td>'+(t.profit>=0?'+':'')+(t.profit/1).toFixed(0)+'</td>'+",
    "    '<td style=\"color:'+(t.ret_pct>=0?'#22c55e':'#ef4444')+'\">'+(t.ret_pct>=0?'+':'')+t.ret_pct+'%</td>'+",
    "    '<td>已平仓</td>'+",
    "    '<td>'+t.reason+'</td>'+",
    "    '<td>'+t.hold_days+'</td>'+",
    "    '</tr>'",
    "  ).join('');",
  ];
  lines.splice(tradeStart, tradeEnd - tradeStart + 1, ...newLines);
  console.log('Trade table replaced');
}

// Find and replace chart tooltip formatter
// Look for "买入: " pattern in the renderSimpleBtChart override
let tpStart = -1, tpEnd = -1;
for (let i = 0; i < lines.length; i++) {
  if (lines[i].includes("html += '<hr style=\"margin:4px 0;border-color:#333\"/>'") &&
      i > 4000) {
    tpStart = i;
    // Find end: the closing } of the if block before "return html;"
    for (let j = i + 1; j < Math.min(i + 30, lines.length); j++) {
      if (lines[j].includes('return html;')) {
        tpEnd = j - 1;
        break;
      }
    }
    console.log(`Tooltip section: lines ${tpStart+1}-${tpEnd+1}`);
    break;
  }
}

if (tpStart >= 0 && tpEnd > tpStart) {
  const newTp = [
    "          html += '<hr style=\"margin:4px 0;border-color:#333\"/>';",
    "          if (day.bought && day.bought.length > 0) {",
    "            html += '<div style=\"color:#22c55e;font-size:11px;margin-bottom:3px\"><b>买入 ' + day.bought.length + '笔:</b></div>';",
    "            day.bought.forEach(function(b) {",
    "              html += '<div style=\"font-size:10px;padding-left:6px;color:#aaa\">' + b.code + (b.name?' ' + b.name:'') + ' @' + b.price + '</div>';",
    "            });",
    "          }",
    "          if (day.sold && day.sold.length > 0) {",
    "            html += '<div style=\"color:#ef4444;font-size:11px;margin-top:4px;margin-bottom:3px\"><b>卖出 ' + day.sold.length + '笔:</b></div>';",
    "            day.sold.forEach(function(s) {",
    "              var sc = s.ret >= 0 ? '#22c55e' : '#ef4444';",
    "              html += '<div style=\"font-size:10px;padding-left:6px\">';",
    "              html += s.code + (s.name?' ' + s.name:'') + ' @' + s.price + ' ';",
    "              html += '<span style=\"color:' + sc + '\">' + (s.ret>=0?'+':'') + s.ret + '%</span> ';",
    "              html += '<span style=\"color:#888\">(' + s.reason + ')</span>';",
    "              html += '</div>';",
    "            });",
    "          }",
  ];
  lines.splice(tpStart, tpEnd - tpStart + 1, ...newTp);
  console.log('Tooltip replaced');
}

fs.writeFileSync(file, lines.join('\n'), 'utf8');
console.log('Done');
