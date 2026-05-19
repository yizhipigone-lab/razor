const fs = require('fs');
const file = 'e:/1target/p9_project/quant-platform/static/js/main.js';
const lines = fs.readFileSync(file, 'utf8').split('\n');

// Find the tooltip formatter section (buy/sell display)
let start = -1, end = -1;
for (let i = 0; i < lines.length; i++) {
  if (lines[i].includes("var day = _simpleBtDailyTrades[dateStr];") && i > 4200) {
    start = i;
    for (let j = i + 1; j < Math.min(i + 30, lines.length); j++) {
      if (lines[j].includes('return html;') && lines[j].trim() === 'return html;') {
        end = j;
        break;
      }
    }
    console.log(`Tooltip section: lines ${start+1} to ${end+1}`);
    break;
  }
}

if (start >= 0 && end > start) {
  const newLines = [
    '        var dateStr = params[0].axisValue;',
    '        if (typeof _simpleBtDailyTrades !== \'undefined\' && _simpleBtDailyTrades[dateStr]) {',
    '          var day = _simpleBtDailyTrades[dateStr];',
    '          var maxShow = 10;',
    '          html += \'<hr style="margin:3px 0;border-color:#333"/>\'',
    '          if (day.bought && day.bought.length > 0) {',
    '            var boughtItems = [];',
    '            var showN = Math.min(day.bought.length, maxShow);',
    '            for (var bi = 0; bi < showN; bi++) {',
    '              var b = day.bought[bi];',
    '              boughtItems.push(b.code + \'@\' + b.price);',
    '            }',
    '            var boughtStr = boughtItems.join(\' \');',
    '            if (day.bought.length > maxShow) boughtStr += \' ...+\' + (day.bought.length - maxShow) + \'只\';',
    '            html += \'<div style="font-size:10px;line-height:1.5;max-width:520px">\';',
    '            html += \'<b style="color:#22c55e">买\' + day.bought.length + \'笔:</b> \' + boughtStr;',
    '            html += \'</div>\';',
    '          }',
    '          if (day.sold && day.sold.length > 0) {',
    '            var soldItems = [];',
    '            var showS = Math.min(day.sold.length, maxShow);',
    '            for (var si = 0; si < showS; si++) {',
    '              var s = day.sold[si];',
    '              var sc = s.ret >= 0 ? \'#22c55e\' : \'#ef4444\';',
    '              var sign = s.ret >= 0 ? \'+\' : \'\';',
    '              soldItems.push(\'<span style="color:\' + sc + \'">\' + s.code + \'@\' + s.price + sign + s.ret + \'%\' + s.reason + \'</span>\');',
    '            }',
    '            var soldStr = soldItems.join(\' \');',
    '            if (day.sold.length > maxShow) soldStr += \' ...+\' + (day.sold.length - maxShow) + \'笔\';',
    '            html += \'<div style="font-size:10px;line-height:1.5;max-width:520px;margin-top:2px">\';',
    '            html += \'<b style="color:#ef4444">卖\' + day.sold.length + \'笔:</b> \' + soldStr;',
    '            html += \'</div>\';',
    '          }',
    '        }',
  ];

  lines.splice(start, end - start + 1, ...newLines);
  console.log('Replaced');
}

fs.writeFileSync(file, lines.join('\n'), 'utf8');
console.log('Done');
