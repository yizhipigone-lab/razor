// Fix main.js: update trade table and chart tooltip
const fs = require('fs');
const file = 'e:/1target/p9_project/quant-platform/static/js/main.js';
let content = fs.readFileSync(file, 'utf8');

// 1. Update trade table rendering (override renderSimpleBtResults)
const oldTable = `document.getElementById('simple-bt-trade-count').textContent = '(' + trades.length + '笔)';
  const tbody = document.getElementById('simple-bt-tbody');
  tbody.innerHTML = trades.slice(-200).reverse().map(t =>
    '<tr><td>'+t.code+'</td><td>'+t.entry_date+'</td><td>'+t.exit_date+'</td>'+
    '<td>'+t.entry_px+'</td><td>'+t.exit_px+'</td>'+
    '<td style="color:'+(t.ret_pct>=0?'#22c55e':'#ef4444')+'">'+(t.ret_pct>=0?'+':'')+t.ret_pct+'%</td>'+
    '<td>'+(t.profit>=0?'+':'')+(t.profit/1).toFixed(0)+'</td>'+
    '<td>'+t.hold_days+'</td><td>'+t.reason+'</td></tr>'
  ).join('');`;

const newTable = `document.getElementById('simple-bt-trade-count').textContent = '(' + trades.length + '笔)';
  const tbody = document.getElementById('simple-bt-tbody');
  tbody.innerHTML = trades.slice(-200).reverse().map(t =>
    '<tr>'+
    '<td>'+t.code+'</td><td>'+(t.name||'')+'</td><td>'+(t.shares||0)+'</td>'+
    '<td>'+t.entry_date+'</td><td>'+t.entry_px+'</td><td>'+(t.entry_total||0)+'</td>'+
    '<td>'+t.exit_date+'</td><td>'+t.exit_px+'</td><td>'+(t.exit_total||0)+'</td>'+
    '<td>'+(t.profit>=0?'+':'')+(t.profit/1).toFixed(0)+'</td>'+
    '<td style="color:'+(t.ret_pct>=0?'#22c55e':'#ef4444')+'">'+(t.ret_pct>=0?'+':'')+t.ret_pct+'%</td>'+
    '<td>已平仓</td>'+
    '<td>'+t.reason+'</td>'+
    '<td>'+t.hold_days+'</td>'+
    '</tr>'
  ).join('');`;

if (content.includes(oldTable)) {
  content = content.replace(oldTable, newTable);
  console.log('Trade table updated');
} else {
  console.log('Trade table NOT FOUND - trying partial match');
  // Fallback: find by unique substring
  const marker = "t.hold_days+'</td><td>'+t.reason+'</td></tr>'";
  if (content.includes(marker)) {
    console.log('Found marker, but need full match');
  }
}

// 2. Update chart tooltip to show name+code and vertical layout
const oldFormatter = `var day = _simpleBtDailyTrades[dateStr];
          html += '<hr style="margin:4px 0;border-color:#333"/>';
          if (day.bought && day.bought.length > 0) {
            html += '<div style="color:#22c55e;font-size:11px">买入: ';
            day.bought.forEach(function(b) { html += b.code + '@' + b.price + ' '; });
            html += '</div>';
          }
          if (day.sold && day.sold.length > 0) {
            html += '<div style="color:#ef4444;font-size:11px">卖出: ';
            day.sold.forEach(function(s) {
              var sc = s.ret >= 0 ? '#22c55e' : '#ef4444';
              html += '<span style="color:' + sc + '">' + s.code + '@' + s.price + '(' + s.reason + (s.ret>=0?'+':'') + s.ret + '%)</span> ';
            });
            html += '</div>';
          }`;

const newFormatter = `var day = _simpleBtDailyTrades[dateStr];
          html += '<hr style="margin:4px 0;border-color:#333"/>';
          if (day.bought && day.bought.length > 0) {
            html += '<div style="color:#22c55e;font-size:11px;margin-bottom:4px"><b>买入 ' + day.bought.length + '笔:</b></div>';
            day.bought.forEach(function(b) {
              html += '<div style="font-size:10px;padding-left:6px;color:#aaa">' + b.code + (b.name?' ' + b.name:'') + ' @' + b.price + '</div>';
            });
          }
          if (day.sold && day.sold.length > 0) {
            html += '<div style="color:#ef4444;font-size:11px;margin-top:4px;margin-bottom:4px"><b>卖出 ' + day.sold.length + '笔:</b></div>';
            day.sold.forEach(function(s) {
              var sc = s.ret >= 0 ? '#22c55e' : '#ef4444';
              html += '<div style="font-size:10px;padding-left:6px">';
              html += '<span>' + s.code + (s.name?' ' + s.name:'') + ' @' + s.price + '</span> ';
              html += '<span style="color:' + sc + '">' + (s.ret>=0?'+':'') + s.ret + '%</span> ';
              html += '<span style="color:#888">(' + s.reason + ')</span>';
              html += '</div>';
            });
          }`;

if (content.includes(oldFormatter)) {
  content = content.replace(oldFormatter, newFormatter);
  console.log('Tooltip formatter updated');
} else {
  console.log('Tooltip formatter NOT FOUND');
}

fs.writeFileSync(file, content, 'utf8');
console.log('Done');
