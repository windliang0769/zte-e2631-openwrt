'use strict';
'require baseclass';
'require rpc';

/* RAX1800Z temperature line inside the "System" information table.
 *
 * luci-mod-status enumerates every *.js in view/status/include, gives each
 * one a load()/render() and re-renders them on the page-wide poll timer.
 * "11_" sorts immediately after "10_system.js", so by the time this render()
 * runs the System table is already in the DOM. We insert one row into that
 * table and return null, which keeps this include from creating a second,
 * empty section of its own. If upstream ever changes the System table beyond
 * recognition, the fallback renders a standalone row instead.
 *
 * The browser cannot read /sys directly: luci-base ACL grants file:list but
 * not file:read. One rpcd/ubus call (see rax1800z_temp) does the sysfs walk
 * server-side, so this file needs no extra privileges. */

var callTemps = rpc.declare({
	object: 'rax1800z_temp',
	method: 'temps'
});

function degree(v) {
	return (v === null || v === undefined) ? _('N/A') : v + '\u00b0C';
}

function temperatureText(data) {
	var wifi = Array.isArray(data.wifi) ? data.wifi : [],
	    slots = Math.max(2, wifi.length),
	    parts = [ _('CPU') + ': ' + degree(data.cpu) ];

	/* PCIe enumerates 14c3:7915 + 14c3:7916 on this board, so keep two
	   slots even when only one radio driver has come up. */
	for (var i = 0; i < slots; i++)
		parts.push(_('WiFi') + i + ': ' + degree(i < wifi.length ? wifi[i] : null));

	return parts.join(', ');
}

function findSystemTable() {
	var tables = document.querySelectorAll('table.table');

	for (var i = 0; i < tables.length; i++) {
		var first = tables[i].querySelector('tr td');
		if (first && first.textContent.trim() === _('Hostname'))
			return tables[i];
	}

	return null;
}

function makeRow(text) {
	var tr = E('tr', { 'class': 'tr' }, [
		E('td', { 'class': 'td left', 'width': '33%' }, [ _('Temperature') ]),
		E('td', { 'class': 'td left' }, [ text ])
	]);

	tr.setAttribute('data-rax1800z-temp', '1');
	return tr;
}

return baseclass.extend({
	title: _('Temperature'),

	load: function() {
		return L.resolveDefault(callTemps(), { cpu: null, wifi: [] });
	},

	render: function(data) {
		var text = temperatureText(data || { cpu: null, wifi: [] }),
		    table = findSystemTable(),
		    rows, anchor = null, cell = null, i, tr, td, tds;

		if (!table)
			return E('table', { 'class': 'table' }, [ makeRow(text) ]);

		rows = table.querySelectorAll('tr');

		for (i = 0; i < rows.length; i++) {
			tr = rows[i];

			if (tr.getAttribute('data-rax1800z-temp') === '1') {
				tds = tr.querySelectorAll('td');
				if (tds.length > 1)
					cell = tds[1];
			}

			td = tr.querySelector('td');
			if (td && td.textContent.trim() === _('Kernel Version'))
				anchor = tr;
		}

		if (cell) {
			cell.textContent = text;
		} else {
			tr = makeRow(text);
			if (anchor)
				table.insertBefore(tr, anchor.nextSibling);
			else
				table.appendChild(tr);
		}

		/* No standalone section content: the row lives in the System table. */
		return null;
	}
});
