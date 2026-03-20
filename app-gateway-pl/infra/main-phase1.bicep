// ============================================================================
// Phase 1: Pure L7 Application Gateway v2 + Private Link
//
// Deploys: VNet (2 subnets), Public IP, Application Gateway v2 (HTTP only),
//          and Private Link configuration on the gateway frontend.
//
// This template has ZERO L4 properties (no `listeners`, `routingRules`, or
// `backendSettingsCollection`). The hypothesis: PE creation will succeed
// because Private Link validation only checks `httpListeners`.
//
// The HTTP listener on port 80 exists solely to satisfy PL validation.
// Backend health will report "Unhealthy" because Aura BC does not speak
// HTTP — this is expected and irrelevant to the experiment.
// ============================================================================

@description('Azure region for all resources')
param location string = 'eastus'

@description('Prefix for resource names')
param prefix string = 'aurabc-appgw'

@description('Neo4j Aura BC FQDN (e.g. xxxxxxxx.databases.neo4j.io)')
param auraFqdn string

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
// Application Gateway v2 — Pure L7 (HTTP only)
//
// No L4 properties: no `listeners`, `routingRules`, `backendSettingsCollection`.
// Only L7: `httpListeners`, `requestRoutingRules`, `backendHttpSettingsCollection`.
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
        name: 'http-port'
        properties: {
          port: 80
        }
      }
    ]

    // --- L7 HTTP listener (satisfies Private Link validation) ---
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
