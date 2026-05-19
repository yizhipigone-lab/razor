const fs = require('fs');
const file = 'e:/1target/p9_project/quant-platform/static/js/main.js';
const lines = fs.readFileSync(file, 'utf8').split('\n');

// Find "var dateStr" line after line 4200
let dsLine = -1;
for (let i = 4200; i < lines.length; i++) {
  if (lines[i].includes("var dateStr = params[0].axisValue;")) {
    dsLine = i;
    break;
  }
}

if (dsLine < 0) { console.log('dateStr not found'); process.exit(1); }
console.log(`dateStr at line ${dsLine+1}`);

// Find end of this section: "}" before "return html;"
let endLine = -1;
for (let i = dsLine; i < Math.min(dsLine + 80, lines.length); i++) {
  if (lines[i].trim() === '}' && i > dsLine + 5) {
    // Check that the NEXT non-empty line is "return html;"
    for (let j = i + 1; j < Math.min(i + 5, lines.length); j++) {
      if (lines[j].trim() === 'return html;') {
        endLine = i;
        break;
      }
      if (lines[j].trim() !== '') break;
    }
    if (endLine > 0) break;
  }
}
if (endLine < 0) { console.log('end not found'); process.exit(1); }
console.log(`end at line ${endLine+1}`);

// Replace
const indent = '        ';
const newLines = [
  indent + 'var dateStr = params[0].axisValue;',
  indent + 'if (typeof _simpleBtDailyTrades !== \'undefined\' && _simpleBtDailyTrades[dateStr]) {',
  indent + '  var day = _simpleBtDailyTrades[dateStr];',
  indent + '  html += \'<hr style="margin:3px 0;border-color:#333"/>\';',
  indent + '  html += \'<div style="max-height:260px;overflow-y:auto;font-size:10px;line-height:1.55">\';',
  indent + '  if (day.bought && day.bought.length > 0) {',
  indent + '    html += \'<div style="color:#22c55e;font-weight:600;margin-bottom:2px">买入 \' + day.bought.length + \' 笔</div>\';',
  indent + '    day.bought.forEach(function(b) {',
  indent + '      html += \'<div style="padding-left:2px;color:#aaa">\' + b.code + (b.name ? \' <span style="color:#ccc">\' + b.name + \'</span>\' : \'\') + \' <span style="color:#ddd">@\' + b.price + \'</span></div>\';',
  indent + '    });',
  indent + '  }',
  indent + '  if (day.sold && day.sold.length > 0) {',
  indent + '    html += \'<div style="color:#ef4444;font-weight:600;margin-top:4px;margin-bottom:2px">卖出 \' + day.sold.length + \' 笔</div>\';',
  indent + '    day.sold.forEach(function(s) {',
  indent + '      var sc = s.ret >= 0 ? \'#22c55e\' : \'#ef4444\';',
  indent + '      html += \'<div style="padding-left:2px">\';',
  indent + '      html += \'<span style="color:#aaa">\' + s.code + \'</span>\';',
  indent + '      if (s.name) html += \' <span style="color:#ccc">\' + s.name + \'</span>\';',
  indent + '      html += \' <span style="color:#ddd">@\' + s.price + \'</span>\';',
  indent + '      html += \' <span style="color:\' + sc + \';font-weight:600">\' + (s.ret >= 0 ? \'+\' : \'\') + s.ret + \'%</span>\';',
  indent + '      html += \' <span style="color:#888;font-size:9px">\' + s.reason + \'</span>\';',
  indent + '      html += \'</div>\';',
  indent + '    });',
  indent + '  }',
  indent + '  html += \'</div>\';',
  indent + '}',
];

lines.splice(dsLine, endLine - dsLine + 1, ...newLines);
console.log(`Replaced ${endLine - dsLine + 1} lines with ${newLines.length} lines`);
fs.writeFileSync(file, lines.join('\n'), 'utf8');
console.log('Done');
