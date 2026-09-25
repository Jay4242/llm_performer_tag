(function () {
  'use strict';

  const PLUGIN_IDS = ['llm_performer_tag', 'LLMPerformerTag'];
  const IMAGE_MENU_ITEM_ID = 'llm-performer-tag-image-menu-item';
  const SCENE_MENU_ITEM_ID = 'llm-performer-tag-scene-menu-item';
  const OPERATIONS_TOGGLE_ID = 'operation-menu';

  function getEntityFromURL() {
    try {
      var imageMatch = window.location.pathname.match(/\/images\/(\d+)/);
      if (imageMatch) return { type: 'image', id: parseInt(imageMatch[1], 10) };
      var imageHash = window.location.hash.match(/\/images\/(\d+)/);
      if (imageHash) return { type: 'image', id: parseInt(imageHash[1], 10) };
      var sceneMatch = window.location.pathname.match(/\/scenes\/(\d+)/);
      if (sceneMatch) return { type: 'scene', id: parseInt(sceneMatch[1], 10) };
      var sceneHash = window.location.hash.match(/\/scenes\/(\d+)/);
      if (sceneHash) return { type: 'scene', id: parseInt(sceneHash[1], 10) };
    } catch (e) {
      console.error('[LLMPerformerTag] Failed to parse entity id from URL:', e);
    }
    return undefined;
  }

  function getBaseURL() {
    var base = document.querySelector('base')?.getAttribute('href') || '/';
    return new URL(base, window.location.href);
  }

  function getGraphqlURL() {
    return new URL('graphql', getBaseURL()).toString();
  }

  function getPluginAssetURL(pluginId, assetPath) {
    return new URL(
      'plugin/' + pluginId + '/assets/' + assetPath,
      getBaseURL()
    ).toString();
  }

  function getStreamAssetURL(pluginId, entityType, entityId, requestId) {
    return new URL(
      'plugin/' + pluginId + '/assets/results/' + entityType + '_' + entityId + '_' + requestId + '_stream.json',
      getBaseURL()
    ).toString();
  }

  async function graphqlRequest(graphqlURL, query, variables) {
    var res = await fetch(graphqlURL, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query, variables: variables }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + res.statusText);
    var json = await res.json();
    if (json.errors?.length) {
      var msg = json.errors.map(function (e) { return e.message; }).join('; ');
      throw new Error(msg || 'GraphQL error');
    }
    return json.data;
  }

  async function resolvePluginId(graphqlURL) {
    var query = 'query { plugins { id name } }';
    try {
      var data = await graphqlRequest(graphqlURL, query);
      if (!data || !data.plugins) return null;

      var plugins = data.plugins;
      for (var i = 0; i < plugins.length; i++) {
        if (PLUGIN_IDS.includes(plugins[i].id)) return plugins[i].id;
      }
      for (var j = 0; j < plugins.length; j++) {
        if (PLUGIN_IDS.includes(plugins[j].name)) return plugins[j].id;
      }
      for (var k = 0; k < plugins.length; k++) {
        var n = (plugins[k].name || '').toLowerCase();
        var id = (plugins[k].id || '').toLowerCase();
        if ((n.includes('llm') || id.includes('llm')) && (n.includes('performer') || id.includes('performer')))
          return plugins[k].id;
      }
      return null;
    } catch (e) {
      console.error('[LLMPerformerTag] Failed to resolve plugin id:', e);
      return null;
    }
  }

  function sleep(ms) {
    return new Promise(function (resolve) { return setTimeout(resolve, ms); });
  }

  async function waitForJobComplete(graphqlURL, jobId, isCancelled) {
    var query = 'query FindJob($input: FindJobInput!) { findJob(input: $input) { status error } }';
    var intervalMs = 1000;
    while (!isCancelled()) {
      var data = await graphqlRequest(graphqlURL, query, { input: { id: jobId } });
      var job = data?.findJob;
      if (job?.status === 'FINISHED') return job;
      if (job?.status === 'FAILED') throw new Error(job?.error || 'LLM task failed.');
      if (job?.status === 'CANCELLED') throw new Error('LLM task cancelled.');
      await sleep(intervalMs);
    }
    throw new Error('LLM task cancelled.');
  }

  async function waitForPerformerResults(pluginId, entityType, entityId, requestId, isCancelled) {
    var url = getPluginAssetURL(pluginId, 'results/' + entityType + '_' + entityId + '_' + requestId + '.json');
    var intervalMs = 500;
    while (!isCancelled()) {
      var res = await fetch(url, { credentials: 'include', cache: 'no-store' });
      if (res.ok) {
        try {
          return await res.json();
        } catch (e) {
          throw new Error('Failed to parse LLM performer results.');
        }
      }
      if (res.status && res.status !== 404) {
        throw new Error('Failed to fetch LLM performer results (HTTP ' + res.status + ')');
      }
      await sleep(intervalMs);
    }
    throw new Error('LLM task cancelled.');
  }

  async function pollStreamResults(pluginId, entityType, entityId, requestId, isCancelled, onProgress) {
    var url = getStreamAssetURL(pluginId, entityType, entityId, requestId);
    var lastReasoning = '';
    var lastOutput = '';
    var intervalMs = 10;

    while (!isCancelled()) {
      try {
        var res = await fetch(url, { credentials: 'include', cache: 'no-store' });
        if (res.ok) {
          var data = await res.json();
          if (data.reasoning !== lastReasoning || data.output !== lastOutput) {
            lastReasoning = data.reasoning || '';
            lastOutput = data.output || '';
            onProgress(lastReasoning, lastOutput);
          }
          if (data.done) break;
        }
      } catch (e) {
        // file not yet available
      }
      await sleep(intervalMs);
    }
  }

  async function findEntityPerformerIds(graphqlURL, entityType, entityId) {
    var query;
    if (entityType === 'image') {
      query = 'query FindImage($id: ID!) { findImage(id: $id) { performers { id name } } }';
    } else {
      query = 'query FindScene($id: ID!) { findScene(id: $id) { performers { id name } } }';
    }
    var data = await graphqlRequest(graphqlURL, query, { id: entityId });
    if (entityType === 'image') {
      return (data?.findImage?.performers ?? []).map(function (p) { return p.id; });
    }
    return (data?.findScene?.performers ?? []).map(function (p) { return p.id; });
  }

  function basenameFromPath(rawPath) {
    if (!rawPath || typeof rawPath !== 'string') return null;
    var path = rawPath;
    if (rawPath.includes('://')) {
      try {
        path = new URL(rawPath).pathname || rawPath;
      } catch (e) {
        path = rawPath;
      }
    }
    var trimmed = path.split('?')[0].split('#')[0];
    var parts = trimmed.split(/[\\/]/);
    return parts[parts.length - 1] || null;
  }

  async function findImageFilename(graphqlURL, imageId) {
    var query = 'query FindImage($id: ID!) { findImage(id: $id) { paths { image } files { path basename } visual_files { ... on ImageFile { path basename } ... on VideoFile { path basename } } } }';
    try {
      var data = await graphqlRequest(graphqlURL, query, { id: imageId });
      var image = data?.findImage || null;
      var path = null;
      var basename = null;
      if (Array.isArray(image?.files) && image.files.length) {
        basename = image.files[0]?.basename || null;
        path = image.files[0]?.path || null;
      }
      if (!basename && Array.isArray(image?.visual_files) && image.visual_files.length) {
        basename = image.visual_files[0]?.basename || null;
      }
      if (!path && Array.isArray(image?.visual_files) && image.visual_files.length) {
        path = image.visual_files[0]?.path || null;
      }
      if (!path) {
        path = image?.paths?.image || null;
      }
      return basename || basenameFromPath(path);
    } catch (e) {
      console.error('[LLMPerformerTag] Failed to resolve image filename:', e);
      return null;
    }
  }

  async function findSceneBasename(graphqlURL, sceneId) {
    var query = 'query FindScene($id: ID!) { findScene(id: $id) { files { path basename } } }';
    try {
      var data = await graphqlRequest(graphqlURL, query, { id: sceneId });
      var scene = data?.findScene || null;
      if (Array.isArray(scene?.files) && scene.files.length) {
        return scene.files[0]?.basename || scene.files[0]?.path || null;
      }
      return null;
    } catch (e) {
      console.error('[LLMPerformerTag] Failed to resolve scene filename:', e);
      return null;
    }
  }

  async function findPerformerMatch(graphqlURL, name) {
    var query = 'query FindPerformers($filter: FindFilterType) { findPerformers(filter: $filter) { performers { id name alias_list disambiguation } } }';
    var data = await graphqlRequest(graphqlURL, query, { filter: { q: name, per_page: 25 } });
    var performers = data?.findPerformers?.performers ?? [];
    var needle = name.trim().toLowerCase();

    // exact name match
    var exact = null;
    for (var i = 0; i < performers.length; i++) {
      if ((performers[i].name || '').toLowerCase() === needle) {
        exact = performers[i];
        break;
      }
    }

    // alias match
    var aliasOwner = null;
    if (!exact) {
      for (var j = 0; j < performers.length; j++) {
        var aliases = performers[j].alias_list || [];
        for (var k = 0; k < aliases.length; k++) {
          if ((aliases[k] || '').toLowerCase() === needle) {
            aliasOwner = performers[j];
            break;
          }
        }
        if (aliasOwner) break;
      }
    }

    return { exact: exact, aliasOwner: aliasOwner };
  }

  async function createPerformer(graphqlURL, name) {
    var mutation = 'mutation PerformerCreate($input: PerformerCreateInput!) { performerCreate(input: $input) { id name } }';
    var data = await graphqlRequest(graphqlURL, mutation, { input: { name: name } });
    return data?.performerCreate;
  }

  async function createPerformerSafe(graphqlURL, name, aliasOwner) {
    try {
      var created = await createPerformer(graphqlURL, name);
      return { id: created?.id || null, created: true };
    } catch (e) {
      console.error('[LLMPerformerTag] Performer create failed:', { name: name, error: e?.message || String(e) });
      if (aliasOwner?.id) {
        console.error('[LLMPerformerTag] Falling back to alias owner performer:', { name: name, aliasOwner: aliasOwner.name, aliasOwnerId: aliasOwner.id });
        return { id: aliasOwner.id, created: false };
      }
      return { id: null, created: false, error: e };
    }
  }

  async function updateEntityPerformers(graphqlURL, entityType, entityId, performerIds) {
    var mutation;
    if (entityType === 'image') {
      mutation = 'mutation ImageUpdate($input: ImageUpdateInput!) { imageUpdate(input: $input) { id } }';
    } else {
      mutation = 'mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }';
    }
    console.error('[LLMPerformerTag] Updating ' + entityType + ' performers:', { entityId: entityId, performerCount: performerIds.length, performerIds: performerIds });
    await graphqlRequest(graphqlURL, mutation, { input: { id: entityId, performer_ids: performerIds } });
  }

  async function runTag(entityType, entityId) {
    var mutation = 'mutation RunPluginTask($plugin_id: ID!, $args_map: Map!, $description: String) { runPluginTask(plugin_id: $plugin_id, args_map: $args_map, description: $description) }';
    var requestId = 'req_' + Date.now() + '_' + Math.random().toString(36).slice(2, 10);
    var graphqlURL = getGraphqlURL();

    var mode, suffix;
    if (entityType === 'image') {
      mode = 'tag_image_performers';
      var filename = await findImageFilename(graphqlURL, entityId);
      suffix = filename || 'image ' + entityId;
    } else {
      mode = 'tag_scene_performers';
      var basename = await findSceneBasename(graphqlURL, entityId);
      suffix = basename || 'scene ' + entityId;
    }

    var args_map = {
      mode: mode,
      image_id: entityType === 'image' ? entityId : undefined,
      scene_id: entityType === 'scene' ? entityId : undefined,
      request_id: requestId,
    };
    var description = 'llm_performer_tag: ' + suffix;
    console.error('[LLMPerformerTag] Task description:', description);

    var resolvedId = await resolvePluginId(graphqlURL);
    if (!resolvedId) {
      console.error('[LLMPerformerTag] Could not resolve plugin id. Aborting to avoid server error.');
      alert('LLM Performer Tag plugin not found on server. Try reloading plugins and refreshing the page.');
      return null;
    }

    try {
      var res = await fetch(graphqlURL, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: mutation,
          variables: { plugin_id: resolvedId, args_map: args_map, description: description },
        }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + res.statusText);
      var json = await res.json();
      if (json.errors) {
        console.error('[LLMPerformerTag] GraphQL errors:', json.errors);
        alert('Failed to start performer tagging. See console for details.');
        return null;
      }
      var jobId = json.data?.runPluginTask || null;
      console.error('[LLMPerformerTag] Tagging queued as job:', jobId);
      return { jobId: jobId, pluginId: resolvedId, requestId: requestId, entityType: entityType };
    } catch (e) {
      console.error('[LLMPerformerTag] Request failed:', e);
      alert('Failed to start performer tagging. See console for details.');
      return null;
    }
  }

  function openTagModal(entityType, entityId) {
    var PluginApi = window.PluginApi;
    if (!PluginApi?.React || !PluginApi?.ReactDOM) {
      alert('LLM Performer Tag: PluginApi is not available in this UI context.');
      return false;
    }
    if (!PluginApi?.libraries?.Bootstrap) {
      alert('LLM Performer Tag: Bootstrap components are not available.');
      return false;
    }

    var React = PluginApi.React;
    var ReactDOM = PluginApi.ReactDOM;
    var Bootstrap = PluginApi.libraries.Bootstrap;
    var Modal = Bootstrap.Modal;
    var Button = Bootstrap.Button;
    var Form = Bootstrap.Form;
    var Spinner = Bootstrap.Spinner;
    var Badge = Bootstrap.Badge;
    if (!Modal || !Button || !Form || !Spinner || !Badge) {
      alert('LLM Performer Tag: Required UI components are missing.');
      return false;
    }

    var container = document.createElement('div');
    container.className = 'llm-performer-tag-modal-container';
    document.body.appendChild(container);

    if (!document.getElementById('llm-performer-tag-modal-styles')) {
      var styleEl = document.createElement('style');
      styleEl.id = 'llm-performer-tag-modal-styles';
      styleEl.textContent =
        '.llm-performer-tag-modal-wrapper { pointer-events: none !important; }' +
        '.llm-performer-tag-modal-dialog { pointer-events: auto; }' +
        '@media (min-width: 1200px) {' +
        '  .llm-performer-tag-modal-dialog {' +
        '    position: fixed !important;' +
        '    left: 0 !important;' +
        '    top: 0 !important;' +
        '    margin: 0 !important;' +
        '    transform: none !important;' +
        '    width: 450px !important;' +
        '    max-width: 450px !important;' +
        '    height: calc(100vh - 4rem) !important;' +
        '    display: flex !important;' +
        '    flex-direction: column !important;' +
        '  }' +
        '  .llm-performer-tag-modal-dialog .modal-content {' +
        '    height: 100%;' +
        '    display: flex;' +
        '    flex-direction: column;' +
        '    border-radius: 0;' +
        '  }' +
        '  .llm-performer-tag-modal-dialog .modal-body {' +
        '    overflow-y: auto;' +
        '    flex: 1;' +
        '  }' +
        '}' +
        '@media (max-width: 1199px) {' +
        '  .llm-performer-tag-modal-dialog {' +
        '    position: fixed !important;' +
        '    bottom: 0 !important;' +
        '    left: 0 !important;' +
        '    right: 0 !important;' +
        '    margin: 0 !important;' +
        '    transform: none !important;' +
        '    max-height: 50vh !important;' +
        '  }' +
        '  .llm-performer-tag-modal-dialog .modal-content {' +
        '    border-radius: 0.5rem 0.5rem 0 0;' +
        '  }' +
        '  .llm-performer-tag-modal-dialog .modal-body {' +
        '    overflow-y: auto;' +
        '    max-height: calc(50vh - 120px);' +
        '  }' +
        '}';
      document.head.appendChild(styleEl);
    }

    function PerformerModal() {
      var useCallback = React.useCallback;
      var useEffect = React.useEffect;
      var useMemo = React.useMemo;
      var useRef = React.useRef;
      var useState = React.useState;
      var Toast = null;
      try {
        Toast = PluginApi.hooks?.useToast ? PluginApi.hooks.useToast() : null;
      } catch (e) {
        Toast = null;
      }
      var loadingState = useState(true);
      var loading = loadingState[0];
      var setLoading = loadingState[1];
      var applyingState = useState(false);
      var applying = applyingState[0];
      var setApplying = applyingState[1];
      var errorState = useState('');
      var error = errorState[0];
      var setError = errorState[1];
      var suggestionsState = useState([]);
      var suggestions = suggestionsState[0];
      var setSuggestions = suggestionsState[1];
      var selectedState = useState([]);
      var selected = selectedState[0];
      var setSelected = selectedState[1];
      var reasoningState = useState('');
      var reasoning = reasoningState[0];
      var setReasoning = reasoningState[1];
      var outputState = useState('');
      var output = outputState[0];
      var setOutput = outputState[1];
      var showReasoningState = useState(false);
      var showReasoning = showReasoningState[0];
      var setShowReasoning = showReasoningState[1];
      var streamBoxRef = useRef(null);
      var autoScrollRef = useRef(true);
      var jobIdRef = useRef(null);

      var handleStreamScroll = useCallback(function () {
        var el = streamBoxRef.current;
        if (!el) return;
        autoScrollRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
      }, []);

      var graphqlURL = useMemo(function () { return getGraphqlURL(); }, []);

      useEffect(function () {
        var cancelled = false;
        async function load() {
          setLoading(true);
          setError('');
          try {
            var job = await runTag(entityType, entityId);
            if (job?.jobId) jobIdRef.current = job.jobId;
            if (!job?.pluginId || !job?.requestId) {
              throw new Error('Failed to queue LLM performer tagging task.');
            }

            var streamPoll = pollStreamResults(
              job.pluginId,
              job.entityType,
              entityId,
              job.requestId,
              function () { return cancelled; },
              function (r, o) {
                if (!cancelled) { setReasoning(r); setOutput(o); }
              }
            );

            if (job.jobId) {
              await waitForJobComplete(graphqlURL, job.jobId, function () { return cancelled; });
            }

            var result = await waitForPerformerResults(
              job.pluginId,
              job.entityType,
              entityId,
              job.requestId,
              function () { return cancelled; }
            );
            var finalReasoning = (result?.reasoning || reasoning);
            var finalOutput = (result?.output || output);
            if (!cancelled) {
              setReasoning(finalReasoning);
              setOutput(finalOutput);
            }
            if (result?.error) {
              throw new Error(result.error);
            }
            var performerNames = (result?.performers || [])
              .map(function (t) { return (t || '').trim(); })
              .filter(function (t) { return t; });
            if (!performerNames.length) {
              throw new Error('No performers returned from LLM task.');
            }
            var enriched = [];
            for (var i = 0; i < performerNames.length; i++) {
              var name = performerNames[i];
              var match = await findPerformerMatch(graphqlURL, name);
              var aliasOwner = !match.exact && match.aliasOwner;
              if (aliasOwner) {
                console.error('[LLMPerformerTag] Alias match resolved:', { name: name, aliasOwner: match.aliasOwner.name, aliasOwnerId: match.aliasOwner.id });
                continue;
              }
              enriched.push({
                uid: name + '::' + (match.exact?.id || 'new') + '::' + enriched.length,
                name: name,
                existingId: match.exact?.id || null,
              });
            }
            if (cancelled) return;
            setSuggestions(enriched);
            setSelected(enriched.map(function (t) { return t.uid; }));
            console.error('[LLMPerformerTag] Suggestions loaded:', enriched);
          } catch (e) {
            if (cancelled) return;
            setError(e?.message || String(e));
          } finally {
            if (!cancelled) setLoading(false);
          }
        }

        load();
        return function () { cancelled = true; };
      }, [graphqlURL]);

      useEffect(function () {
        var el = streamBoxRef.current;
        if (!el || !autoScrollRef.current) return;
        requestAnimationFrame(function () {
          var recheck = streamBoxRef.current;
          if (!recheck) return;
          recheck.scrollTop = recheck.scrollHeight;
        });
      }, [reasoning, output]);

      function toggleSelected(uid) {
        setSelected(function (prev) {
          if (prev.includes(uid)) return prev.filter(function (t) { return t !== uid; });
          return prev.concat([uid]);
        });
      }

      async function applyPerformers() {
        if (!selected.length) {
          Toast?.info?.('No performers selected');
          return;
        }
        setApplying(true);
        setError('');
        try {
          var currentPerformerIds = await findEntityPerformerIds(graphqlURL, entityType, entityId);
          var performerIds = currentPerformerIds.slice();
          console.error('[LLMPerformerTag] Current ' + entityType + ' performers:', currentPerformerIds);
          console.error('[LLMPerformerTag] Selected performers:', selected);
          for (var i = 0; i < suggestions.length; i++) {
            var suggestion = suggestions[i];
            if (!selected.includes(suggestion.uid)) continue;
            var performerId = suggestion.existingId;
            if (!performerId) {
              console.error('[LLMPerformerTag] Creating performer:', suggestion.name);
              var created = await createPerformerSafe(graphqlURL, suggestion.name, null);
              performerId = created.id;
              console.error('[LLMPerformerTag] Created performer:', { name: suggestion.name, id: performerId, created: created.created });
            }
            console.error('[LLMPerformerTag] Using performer:', { name: suggestion.name, id: performerId });
            if (performerId && !performerIds.includes(performerId)) {
              performerIds.push(performerId);
            }
          }
          var deduped = Array.from(new Set(performerIds));
          console.error('[LLMPerformerTag] Final performer id list:', deduped);
          await updateEntityPerformers(graphqlURL, entityType, entityId, deduped);
          var after = await findEntityPerformerIds(graphqlURL, entityType, entityId);
          console.error('[LLMPerformerTag] Performers after update:', after);
          Toast?.success?.('Performers applied');
          onClose();
        } catch (e) {
          setError(e?.message || String(e));
          Toast?.error?.(e);
        } finally {
          setApplying(false);
        }
      }

      var empty = !loading && !suggestions.length;

      async function handleClose() {
        if (jobIdRef.current) {
          try {
            await graphqlRequest(graphqlURL,
              'mutation StopJob($job_id: ID!) { stopJob(job_id: $job_id) }',
              { job_id: jobIdRef.current }
            );
          } catch (e) {
            // ignore stop errors
          }
        }
        onClose();
      }

      return React.createElement(
        Modal,
        { show: true, onHide: handleClose, backdrop: false, keyboard: false, className: 'llm-performer-tag-modal-wrapper', dialogClassName: 'llm-performer-tag-modal-dialog' },
        React.createElement(
          Modal.Header,
          { closeButton: true },
          React.createElement(Modal.Title, null, 'LLM Suggested Performers')
        ),
        React.createElement(
          Modal.Body,
          null,
          loading
            ? React.createElement(
                'div',
                null,
                React.createElement(
                  'div',
                  { className: 'd-flex align-items-center mb-3' },
                  React.createElement(Spinner, { animation: 'border', role: 'status', className: 'mr-3' }),
                  React.createElement('span', null, 'Running LLM performer task...')
                ),
                (reasoning || output)
                  ? React.createElement(
                      'div',
                      {
                        ref: streamBoxRef,
                        onScroll: handleStreamScroll,
                        className: 'border rounded p-2 mb-3',
                        style: {
                          maxHeight: '300px', overflowY: 'auto', background: '#1a1a2e',
                          fontFamily: 'monospace', fontSize: '0.85rem',
                          whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: '#c0c0c0',
                        },
                      },
                      reasoning
                        ? React.createElement('div', { style: { color: '#8888cc' } },
                            React.createElement('strong', null, 'Thinking...'),
                            React.createElement('br'), reasoning)
                        : null,
                      output
                        ? React.createElement('div', {
                            style: { marginTop: reasoning ? '0.5rem' : '0',
                              borderTop: reasoning ? '1px solid #444' : 'none',
                              paddingTop: reasoning ? '0.5rem' : '0', color: '#a0d0a0' },
                          }, reasoning ? React.createElement('strong', null, 'Output:') : null,
                            reasoning ? React.createElement('br') : null, output)
                        : null
                    )
                  : null
              )
            : null,
          error ? React.createElement('div', { className: 'text-danger mb-3' }, error) : null,
          empty ? React.createElement('div', { className: 'text-muted' }, 'No performer suggestions available.') : null,
          !loading && suggestions.length
            ? React.createElement(
                'div',
                null,
                React.createElement(
                  'div',
                  { className: 'mb-2 d-flex align-items-center' },
                  React.createElement(Button, { variant: 'secondary', size: 'sm', className: 'mr-2',
                    onClick: function () { setSelected(suggestions.map(function (t) { return t.uid; })); } },
                    'Select all'),
                  React.createElement(Button, { variant: 'secondary', size: 'sm',
                    onClick: function () { setSelected([]); } },
                    'Clear')
                ),
                suggestions.map(function (t) {
                  return React.createElement(
                    Form.Check,
                    {
                      key: t.uid, type: 'checkbox', className: 'mb-2',
                      checked: selected.includes(t.uid),
                      onChange: function () { toggleSelected(t.uid); },
                      label: React.createElement(
                        'span',
                        null,
                        t.name,
                        t.existingId
                          ? React.createElement(Badge, { variant: 'secondary', className: 'ml-2' }, 'existing')
                          : React.createElement(Badge, { variant: 'secondary', className: 'ml-2' }, 'new')
                      ),
                    }
                  );
                })
              )
            : null,
          !loading && (reasoning || output)
            ? React.createElement(
                'div',
                { className: 'mt-3' },
                React.createElement(Button, { variant: 'link', size: 'sm', className: 'p-0',
                  onClick: function () { setShowReasoning(!showReasoning); },
                  style: { textDecoration: 'none' } },
                  showReasoning ? 'Hide thinking/output' : 'Show thinking/output'),
                showReasoning
                  ? React.createElement(
                      'div',
                      { className: 'border rounded p-2 mt-1', style: { maxHeight: '300px', overflowY: 'auto',
                        background: '#1a1a2e', fontFamily: 'monospace', fontSize: '0.85rem',
                        whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: '#c0c0c0' } },
                      reasoning ? React.createElement('div', { style: { color: '#8888cc' } },
                        React.createElement('strong', null, 'Thinking:'), React.createElement('br'), reasoning) : null,
                      output ? React.createElement('div', {
                        style: { marginTop: reasoning ? '0.5rem' : '0',
                          borderTop: reasoning ? '1px solid #444' : 'none',
                          paddingTop: reasoning ? '0.5rem' : '0', color: '#a0d0a0' },
                      }, reasoning ? React.createElement('strong', null, 'Output:') : null,
                        reasoning ? React.createElement('br') : null, output) : null
                    )
                  : null
              )
            : null
        ),
        React.createElement(
          Modal.Footer,
          null,
          React.createElement(Button, { variant: 'secondary', onClick: handleClose, disabled: applying }, 'Cancel'),
          React.createElement(Button, { variant: 'primary', onClick: applyPerformers,
            disabled: applying || loading || !selected.length },
            applying ? 'Applying...' : 'Apply Performers')
        )
      );
    }

    function onClose() {
      ReactDOM.unmountComponentAtNode(container);
      container.remove();
    }

    try {
      ReactDOM.render(React.createElement(PerformerModal), container);
    } catch (e) {
      console.error('[LLMPerformerTag] Failed to render modal:', e);
      alert('LLM Performer Tag: failed to render modal. ' + (e?.message || String(e)));
      onClose();
      return false;
    }
    return true;
  }

  function closeDropdown(menuEl) {
    var dropdown = menuEl?.closest('.dropdown');
    menuEl?.classList.remove('show');
    dropdown?.classList.remove('show');
  }

  function createMenuItem(menuEl, entityType) {
    if (!menuEl) return;
    var menuItemId = entityType === 'image' ? IMAGE_MENU_ITEM_ID : SCENE_MENU_ITEM_ID;
    var existing = document.getElementById(menuItemId);
    if (existing) {
      if (menuEl.contains(existing)) return;
      existing.remove();
    }

    var item = document.createElement('button');
    item.id = menuItemId;
    item.type = 'button';
    item.className = 'dropdown-item bg-secondary text-white';
    item.textContent = entityType === 'image' ? 'Tag performers (LLM)' : 'Tag performers (LLM)';
    item.addEventListener('click', function (ev) {
      ev.preventDefault();
      console.error('[LLMPerformerTag] Menu item clicked for ' + entityType);
      var entity = getEntityFromURL();
      if (!entity) {
        alert('LLM Performer Tag: could not determine entity id from URL.');
        return;
      }
      if (!openTagModal(entity.type, entity.id)) {
        runTag(entity.type, entity.id);
      }
      closeDropdown(menuEl);
    });
    item.style.cursor = 'pointer';

    var items = Array.from(menuEl.querySelectorAll('.dropdown-item'));
    var defaultThumbItem = items.find(function (el) {
      var text = (el.textContent || '').trim().toLowerCase();
      return text.includes('generate default thumbnail');
    });
    if (defaultThumbItem?.parentElement === menuEl) {
      defaultThumbItem.insertAdjacentElement('afterend', item);
    } else {
      var deleteItem = items.find(function (el) {
        var text = (el.textContent || '').trim().toLowerCase();
        return text.includes('delete');
      });
      if (deleteItem?.parentElement === menuEl) {
        menuEl.insertBefore(item, deleteItem);
      } else {
        menuEl.appendChild(item);
      }
    }
  }

  function findOperationsMenu() {
    var toggle = document.getElementById(OPERATIONS_TOGGLE_ID);
    if (!toggle) return null;
    var dropdown = toggle.closest('.dropdown');
    if (!dropdown) return null;
    var menuEl = dropdown.querySelector('.dropdown-menu');
    if (!menuEl) return null;
    return menuEl;
  }

  function mountIfPossible() {
    var entity = getEntityFromURL();
    if (!entity) return false;
    var menuEl = findOperationsMenu();
    if (!menuEl) return false;
    createMenuItem(menuEl, entity.type);
    return true;
  }

  // Image task registration
  if (typeof window.registerTask === 'function') {
    window.registerTask({
      name: 'Tag performers (LLM)',
      description: 'Identify performers in the current ' +
        (getEntityFromURL()?.type === 'scene' ? 'scene' : 'image') +
        ' using a vision LLM',
      icon: 'fa-user-tag',
      handler: async function () {
        var entity = getEntityFromURL();
        if (!entity) {
          alert('LLM Performer Tag: could not determine entity id from URL.');
          return;
        }
        if (!openTagModal(entity.type, entity.id)) {
          await runTag(entity.type, entity.id);
        }
      },
    });
    console.error('[LLMPerformerTag] Task registered via registerTask');
  } else {
    mountIfPossible();
    var observer = new MutationObserver(function (mutationsList) {
      for (var i = 0; i < mutationsList.length; i++) {
        var mutation = mutationsList[i];
        for (var j = 0; j < mutation.addedNodes.length; j++) {
          var addedNode = mutation.addedNodes[j];
          if (addedNode.nodeType !== Node.ELEMENT_NODE) continue;
          if (
            addedNode.id === OPERATIONS_TOGGLE_ID ||
            addedNode.querySelector?.('#' + OPERATIONS_TOGGLE_ID) ||
            addedNode.classList?.contains('dropdown-menu')
          ) {
            mountIfPossible();
            return;
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  console.error('[LLMPerformerTag] UI script initialized');
})();
