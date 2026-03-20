// ============================================================================
// Phase 2: L7 + L4 Application Gateway v2 + Private Link
//
// Deploys the same gateway as Phase 1, but adds L4 TCP listeners on port 7687.
// Deployed as an incremental update to the Phase 1 gateway.
//
// The L7 HTTP listener on port 80 remains in place (removing it might
// invalidate the Private Link configuration).
//
// The experiment: does the Private Link tunnel established in Phase 1
// continue to forward TCP traffic after L4 listeners are added?
// ============================================================================

@description('Azure region for all resources')
param location string = 'eastus'

@description('Prefix for resource names')
param prefix string = 'aurabc-appgw'

@description('Neo4j Aura BC FQDN (e.g. xxxxxxxx.databases.neo4j.io)')
param auraFqdn string

@description('Neo4j Aura BC port')
param auraPort int = 7687

// ---------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------
var vnetName = '${prefix}-vnet'
var publicIpName = '${prefix}-pip'
var appGwName = '${prefix}-gw'
var plConfigName = 'pl-config'

// ---------------------------------------------------------------------------
// Public IP for Application Gateway (static, Standard SKU)
// ---------------------------------------------------------------------------
resource publicIp 'Microsoft.Network/publicIPAddresses@2025-01-01' = {
  name: publicIpName
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAddressVersion: 'IPv4'
    publicIPAllocationMethod: 'Static'
    idleTimeoutInMinutes: 4
  }
}

// ---------------------------------------------------------------------------
// Virtual Network with 2 subnets
// ---------------------------------------------------------------------------
resource vnet 'Microsoft.Network/virtualNetworks@2025-01-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/16'
      ]
    }
  }

  resource appGwSubnet 'subnets' = {
    name: 'appgw-subnet'
    properties: {
      addressPrefix: '10.0.1.0/24'
      delegations: [
        {
          name: 'appgw-delegation'
          properties: {
            serviceName: 'Microsoft.Network/applicationGateways'
          }
        }
      ]
    }
  }

  resource plSubnet 'subnets' = {
    name: 'appgw-pl-subnet'
    properties: {
      addressPrefix: '10.0.2.0/24'
      privateLinkServiceNetworkPolicies: 'Disabled'
    }
    dependsOn: [appGwSubnet]
  }
}

// ---------------------------------------------------------------------------
// Application Gateway v2 — L7 + L4 Hybrid
//
// L7 HTTP listener on port 80 (keeps PL validation happy).
// L4 TCP listener on port 7687 (carries actual Bolt traffic).
// Private Link configuration on the frontend IP.
// ---------------------------------------------------------------------------
resource appGw 'Microsoft.Network/applicationGateways@2025-01-01' = {
  name: appGwName
  location: location
  properties: {
    sku: {
      name: 'Standard_v2'
      tier: 'Standard_v2'
    }
    autoscaleConfiguration: {
      minCapacity: 1
      maxCapacity: 2
    }
    gatewayIPConfigurations: [
      {
        name: 'appgw-ip-config'
        properties: {
          subnet: {
            id: vnet::appGwSubnet.id
          }
        }
      }
    ]
    frontendIPConfigurations: [
      {
        name: 'appgw-frontend-ip'
        properties: {
          publicIPAddress: {
            id: publicIp.id
          }
          privateLinkConfiguration: {
            id: resourceId('Microsoft.Network/applicationGateways/privateLinkConfigurations', appGwName, plConfigName)
          }
        }
      }
    ]
    frontendPorts: [
      {
        name: 'bolt-port'
        properties: {
          port: auraPort
        }
      }
      {
        name: 'http-port'
        properties: {
          port: 80
        }
      }
    ]

    // --- L7 HTTP listener (keeps PL validation intact) ---
    httpListeners: [
      {
        name: 'pl-http-listener'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', appGwName, 'appgw-frontend-ip')
          }
          frontendPort: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', appGwName, 'http-port')
          }
          protocol: 'Http'
        }
      }
    ]

    backendHttpSettingsCollection: [
      {
        name: 'pl-http-backend-setting'
        properties: {
          port: 80
          protocol: 'Http'
          requestTimeout: 30
        }
      }
    ]

    requestRoutingRules: [
      {
        name: 'pl-http-routing-rule'
        properties: {
          priority: 200
          ruleType: 'Basic'
          httpListener: {
            id: resourceId('Microsoft.Network/applicationGateways/httpListeners', appGwName, 'pl-http-listener')
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', appGwName, 'aura-bc-backend-pool')
          }
          backendHttpSettings: {
            id: resourceId('Microsoft.Network/applicationGateways/backendHttpSettingsCollection', appGwName, 'pl-http-backend-setting')
          }
        }
      }
    ]

    // --- L4 TCP listener (carries actual Bolt traffic) ---
    listeners: [
      {
        name: 'bolt-tcp-listener'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', appGwName, 'appgw-frontend-ip')
          }
          frontendPort: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', appGwName, 'bolt-port')
          }
          protocol: 'Tcp'
        }
      }
    ]

    backendAddressPools: [
      {
        name: 'aura-bc-backend-pool'
        properties: {
          backendAddresses: [
            {
              fqdn: auraFqdn
            }
          ]
        }
      }
    ]

    backendSettingsCollection: [
      {
        name: 'bolt-tcp-backend-setting'
        properties: {
          port: auraPort
          protocol: 'Tcp'
          timeout: 300
        }
      }
    ]

    routingRules: [
      {
        name: 'bolt-routing-rule'
        properties: {
          priority: 100
          ruleType: 'Basic'
          listener: {
            id: resourceId('Microsoft.Network/applicationGateways/listeners', appGwName, 'bolt-tcp-listener')
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', appGwName, 'aura-bc-backend-pool')
          }
          backendSettings: {
            id: resourceId('Microsoft.Network/applicationGateways/backendSettingsCollection', appGwName, 'bolt-tcp-backend-setting')
          }
        }
      }
    ]

    // --- Private Link configuration ---
    privateLinkConfigurations: [
      {
        name: plConfigName
        properties: {
          ipConfigurations: [
            {
              name: 'pl-ip-config'
              properties: {
                primary: true
                privateIPAllocationMethod: 'Dynamic'
                subnet: {
                  id: vnet::plSubnet.id
                }
              }
            }
          ]
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output appGatewayResourceId string = appGw.id
output appGatewayPublicIp string = publicIp.properties.ipAddress
output privateLinkConfigName string = plConfigName
output appGatewayName string = appGw.name
output resourceGroupName string = resourceGroup().name
