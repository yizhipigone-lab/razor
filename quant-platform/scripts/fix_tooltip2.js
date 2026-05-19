const fs = require('fs');
const file = 'e:/1target/p9_project/quant-platform/static/js/main.js';
let content = fs.readFileSync(file, 'utf8');

const oldSection = `var dateStr = params[0].axisValue;
        if (typeof _simpleBtDailyTrades !== 'undefined' && _simpleBtDailyTrades[dateStr]) {
          var day = _simpleBtDailyTrades[dateStr];
          var maxShow = 10;
          html += '<hr style="margin:3px 0;border-color:#333"/>'
          if (day.bought && day.bought.length > 0) {
            var boughtItems = [];
            var showN = Math.min(day.bought.length, maxShow);
            for (var bi = 0; bi < showN; bi++) {
              var b = day.bought[bi];
              boughtItems.push(b.code + '@' + b.price);
            }
            var boughtStr = boughtItems.join(' ');
            if (day.bought.length > maxShow) boughtStr += ' ...+' + (day.bought.length - maxShow) + '只';
            html += '<div style="font-size:10px;line-height:1.5;max-width:520px">';
            html += '<b style="color:#22c55e">买' + day.bought.length + '笔:</b> ' + boughtStr;
            html += '</div>';
          }
          if (day.sold && day.sold.length > 0) {
            var soldItems = [];
            var showS = Math.min(day.sold.length, maxShow);
            for (var si = 0; si < showS; si++) {
              var s = day.sold[si];
              var sc = s.ret >= 0 ? '#22c55e' : '#ef4444';
              var sign = s.ret >= 0 ? '+' : '';
              soldItems.push('<span style="color:' + sc + '">' + s.code + '@' + s.price + sign + s.ret + '%' + s.reason + '</span>');
            }
            var soldStr = soldItems.join(' ');
            if (day.sold.length > maxShow) soldStr += ' ...+' + (day.sold.length - maxShow) + '笔';
            html += '<div style="font-size:10px;line-height:1.5;max-width:520px;margin-top:2px">';
            html += '<b style="color:#ef4444">卖' + day.sold.length + '笔:</b> ' + soldStr;
            html += '</div>';
          }
        }`;

const newSection = `var dateStr = params[0].axisValue;
        if (typeof _simpleBtDailyTrades !== 'undefined' && _simpleBtDailyTrades[dateStr]) {
          var day = _simpleBtDailyTrades[dateStr];
          html += '<hr style="margin:3px 0;border-color:#333"/>';
          html += '<div style="max-height:280px;overflow-y:auto;font-size:10px;line-height:1.6">';
          // 买入
          if (day.bought && day.bought.length > 0) {
            html += '<div style="color:#22c55e;font-weight:600;margin-bottom:1px">买入 ' + day.bought.length + ' 笔</div>';
            day.bought.forEach(function(b) {
              html += '<div style="padding-left:2px;color:#aaa">' + b.code + (b.name ? ' <span style="color:#ccc">' + b.name + '</span>' : '') + ' <span style="color:#ddd">@' + b.price + '</span></div>';
            });
          }
          // 卖出
          if (day.sold && day.sold.length > 0) {
            html += '<div style="color:#ef4444;font-weight:600;margin-top:3px;margin-bottom:1px">卖出 ' + day.sold.length + ' 笔</div>';
            day.sold.forEach(function(s) {
              var sc = s.ret >= 0 ? '#22c55e' : '#ef4444';
              html += '<div style="padding-left:2px">';
              html += '<span style="color:#aaa">' + s.code + '</span>';
              if (s.name) html += ' <span style="color:#ccc">' + s.name + '</span>';
              html += ' <span style="color:#ddd">@' + s.price + '</span>';
              html += ' <span style="color:' + sc + ';font-weight:600">' + (s.ret >= 0 ? '+' : '') + s.ret + '%</span>';
              html += ' <span style="color:#888;font-size:9px">' + s.reason + '</span>';
              html += '</div>';
            });
          }
          html += '</div>';
        }`;

if (content.includes(oldSection)) {
  content = content.replace(oldSection, newSection);
  console.log('Replaced');
} else {
  console.log('NOT FOUND - trying partial match');
  // Try to find by unique substrings
  if (content.includes('var dateStr = params[0].axisValue;')) {
    console.log('Found dateStr line');
  }
}

fs.writeFileSync(file, content, 'utf8');
console.log('Done');
