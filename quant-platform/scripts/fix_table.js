const fs = require('fs');
const file = 'e:/1target/p9_project/quant-platform/static/js/main.js';
const lines = fs.readFileSync(file, 'utf8').split('\n');

// Fix table rendering - format numbers
for (let i = 0; i < lines.length; i++) {
  if (lines[i].includes("tbody.innerHTML = trades.slice(-200).reverse().map(t =>") &&
      i > 3900) {
    console.log(`Table render at line ${i+1}`);

    // Find the end of the render (ends with .join('');)
    let endLine = i;
    for (let j = i; j < Math.min(i + 20, lines.length); j++) {
      if (lines[j].includes(".join('');")) {
        endLine = j;
        break;
      }
    }

    const newLines = [
      "  tbody.innerHTML = trades.slice(-200).reverse().map(t =>",
      "    '<tr>'+",
      "    '<td>'+t.code+'</td><td>'+(t.name||'')+'</td><td>'+(t.shares||0)+'</td>'+",
      "    '<td>'+t.entry_date+'</td><td>'+t.entry_px+'</td><td>'+(t.entry_total||0).toLocaleString()+'</td>'+",
      "    '<td>'+t.exit_date+'</td><td>'+t.exit_px+'</td><td>'+(t.exit_total||0).toLocaleString()+'</td>'+",
      "    '<td style=\"color:'+(t.profit>=0?'#22c55e':'#ef4444')+'\">'+(t.profit>=0?'+':'')+Math.abs(t.profit).toFixed(0)+'</td>'+",
      "    '<td style=\"color:'+(t.ret_pct>=0?'#22c55e':'#ef4444')+'\">'+(t.ret_pct>=0?'+':'')+t.ret_pct+'%</td>'+",
      "    '<td>已平仓</td>'+",
      "    '<td style=\"font-size:10px;color:var(--text2)\">'+t.reason+'</td>'+",
      "    '<td>'+t.hold_days+'</td>'+",
      "    '</tr>'",
      "  ).join('');",
    ];

    lines.splice(i, endLine - i + 1, ...newLines);
    console.log(`Replaced lines ${i+1} to ${endLine+1}`);
    break;
  }
}

fs.writeFileSync(file, lines.join('\n'), 'utf8');
console.log('Done');
