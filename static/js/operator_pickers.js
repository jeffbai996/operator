/* The selects retain their values and change handlers; one themed popover
   replaces platform popup paint without changing the closed controls. */
(() => {
  'use strict';
  function install(root, selects) {
    if (!root || root.dataset.pickerMenus) return;
    root.dataset.pickerMenus = '1';
    const menu = document.createElement('div');
    menu.id = 'op-picker-menu';
    menu.className = 'op-picker-menu';
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('popover', 'auto');
    menu.hidden = true;
    root.appendChild(menu);
    let active = null, rows = [], cursor = -1, typed = '', typedAt = 0;
    const labels = {'op-model': 'Model', 'op-effort': 'Effort', 'op-speed': 'Processing speed'};

    function close(restoreFocus = false) {
      const select = active;
      active = null;
      if (select) {
        select.setAttribute('aria-expanded', 'false');
        select.removeAttribute('aria-activedescendant');
      }
      if (menu.hidePopover && menu.matches(':popover-open')) menu.hidePopover();
      menu.hidden = true;
      if (restoreFocus && select && !select.disabled) select.focus({preventScroll: true});
    }
    function highlight(index) {
      cursor = index;
      rows.forEach((row, i) => row.element.classList.toggle('is-active', i === cursor));
      const row = rows[cursor];
      if (!row || !active) return;
      active.setAttribute('aria-activedescendant', row.element.id);
      const top = row.element.offsetTop, bottom = top + row.element.offsetHeight;
      if (top < menu.scrollTop) menu.scrollTop = top;
      else if (bottom > menu.scrollTop + menu.clientHeight) menu.scrollTop = bottom - menu.clientHeight;
    }
    function choose(index) {
      const select = active, row = rows[index];
      if (!select || !row || row.disabled || select.disabled) return;
      select.selectedIndex = row.index;
      close(true);
      select.dispatchEvent(new Event('input', {bubbles: true}));
      select.dispatchEvent(new Event('change', {bubbles: true}));
    }
    function open(select) {
      if (select.disabled || select.hidden || !select.getClientRects().length) return;
      if (active === select) { close(true); return; }
      close();
      active = select; typed = ''; typedAt = 0;
      menu.replaceChildren();
      menu.setAttribute('aria-label', labels[select.id]);
      rows = [];
      Array.from(select.options).forEach((option, index) => {
        if (option.hidden) return;
        const element = document.createElement('div');
        const disabled = option.disabled || option.parentElement.disabled === true;
        element.className = 'op-picker-option';
        element.id = 'op-picker-option-' + index;
        element.setAttribute('role', 'option');
        element.setAttribute('aria-label', option.textContent);
        element.setAttribute('aria-selected', String(option.selected));
        element.setAttribute('aria-disabled', String(disabled));
        element.textContent = option.textContent;
        const position = rows.length;
        element.addEventListener('pointermove', () => { if (!disabled) highlight(position); });
        element.addEventListener('pointerdown', event => event.preventDefault());
        element.addEventListener('click', () => choose(position));
        rows.push({element, index, disabled});
        menu.appendChild(element);
      });
      if (!rows.length) { close(); return; }
      select.setAttribute('aria-expanded', 'true');
      select.focus({preventScroll: true});
      const viewport = window.visualViewport;
      const left = viewport ? viewport.offsetLeft : 0, top = viewport ? viewport.offsetTop : 0;
      const width = viewport ? viewport.width : window.innerWidth;
      const height = viewport ? viewport.height : window.innerHeight;
      menu.style.maxWidth = Math.max(0, width - 24) + 'px';
      menu.style.maxHeight = Math.min(360, height - 24) + 'px';
      menu.hidden = false;
      if (menu.showPopover) menu.showPopover();
      const anchor = select.getBoundingClientRect(), box = menu.getBoundingClientRect();
      const above = anchor.top - top - 18, below = top + height - anchor.bottom - 18;
      const upwards = above >= box.height || above >= below;
      const available = Math.max(0, upwards ? above : below);
      const menuHeight = Math.min(box.height, available);
      menu.style.maxHeight = Math.min(360, available) + 'px';
      const alignedLeft = select.id === 'op-model' ? anchor.left : anchor.right - box.width;
      menu.style.left = Math.max(left + 12, Math.min(alignedLeft, left + width - box.width - 12)) + 'px';
      menu.style.top = (upwards ? anchor.top - menuHeight - 6 : anchor.bottom + 6) + 'px';
      const selected = rows.findIndex(row => row.index === select.selectedIndex && !row.disabled);
      highlight(selected >= 0 ? selected : rows.findIndex(row => !row.disabled));
    }
    function move(direction) {
      for (let step = 1; step <= rows.length; step++) {
        const index = (cursor + direction * step + rows.length) % rows.length;
        if (!rows[index].disabled) { highlight(index); break; }
      }
    }
    selects.filter(Boolean).forEach(select => {
      select.setAttribute('aria-label', labels[select.id]);
      select.setAttribute('aria-haspopup', 'listbox');
      select.setAttribute('aria-controls', menu.id);
      select.setAttribute('aria-expanded', 'false');
      select.addEventListener('pointerdown', event => {
        if (event.button === 0 && !select.disabled) {
          event.preventDefault();
          select.focus({preventScroll: true});
        }
      });
      select.addEventListener('mousedown', event => event.preventDefault());
      select.addEventListener('pointerup', event => {
        if (event.button === 0 && !select.disabled) {
          event.preventDefault(); open(select);
        }
      });
      // WebKit suppresses the compatibility click after touch pointerdown is
      // canceled. Pointerup handles real pointers; detail=0 preserves clicks
      // from assistive technology and programmatic activation.
      select.addEventListener('click', event => {
        event.preventDefault();
        if (event.detail === 0) open(select);
      });
      select.addEventListener('keydown', event => {
        const key = event.key;
        if (key === 'Tab') { close(); return; }
        if (key === 'Escape' && active === select) {
          event.preventDefault(); event.stopPropagation(); close(true); return;
        }
        if (['Enter', ' ', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'F4'].includes(key)) {
          event.preventDefault(); event.stopPropagation();
          if (active !== select) { open(select); return; }
          if (key === 'Enter' || key === ' ') choose(cursor);
          else if (key === 'F4') close(true);
          else if (key === 'Home') highlight(rows.findIndex(row => !row.disabled));
          else if (key === 'End') highlight(rows.findLastIndex(row => !row.disabled));
          else move(key === 'ArrowUp' ? -1 : 1);
        } else if (key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
          event.preventDefault();
          if (active !== select) open(select);
          if (active !== select) return;
          typed = (event.timeStamp - typedAt < 700 ? typed : '') + key.toLowerCase();
          typedAt = event.timeStamp;
          const match = rows.findIndex(row => !row.disabled && row.element.textContent.toLowerCase().startsWith(typed));
          if (match >= 0) highlight(match);
        }
      });
      select.addEventListener('change', () => { if (active === select) close(); });
      select.addEventListener('blur', () => { if (active === select) close(); });
      new MutationObserver(records => {
        if (active !== select) return;
        if (select.disabled || select.hidden || !select.getClientRects().length || !select.options.length) {
          close();
          return;
        }
        if (records.some(r => r.type === 'childList')) close();
      }).observe(select,
        {childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'hidden']});
    });
    menu.addEventListener('toggle', event => { if (event.newState === 'closed') close(); });
    document.addEventListener('pointerdown', event => {
      if (active && !menu.contains(event.target) && event.target !== active) close();
    }, true);
    document.addEventListener('scroll', event => {
      if (!active || menu.contains(event.target)) return;
      const t = event.target;
      if (t === document || t === document.documentElement || t === document.body || (t instanceof Node && t.contains(active))) {
        close();
      }
    }, true);
    window.addEventListener('resize', () => close());
    document.addEventListener('visibilitychange', () => { if (document.hidden) close(); });
  }
  window.OperatorPickerMenus = {install};
})();
